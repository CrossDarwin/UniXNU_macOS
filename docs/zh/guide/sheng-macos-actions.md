# 用 Actions 构建 sheng macOS 测试包

`sheng macOS test package` 工作流为 256 GB sheng 构建 macOS 27.3（`26C5035r`）。
工作流使用 `xcode-27` ARM64 macOS runner，设备与 UEFI 仓库固定到指定提交。

工作流选用配套的 SM8550 和 USB 版本。`scripts/ci/sheng_sources.py` 将 SoC 覆盖
写入设备的 `local.toml`，USB 单独检出固定提交。两个版本均纳入缓存标识和
`test-package.json`。

## 构建

在仓库 Actions Secrets 中设置 `UNIXNU_UNIQUE_CHIP_ID`，值为本次安装使用的
8 位十六进制平台种子。后续构建保留该值。

在 `sheng/macos-actions` 分支手动运行工作流。首次安装且没有缓存的 Data 种子时，
勾选 `init_data`。后续构建复用种子。推送提交不触发构建。

准备系统镜像前，工作流从[固定版本的 linux-firmware](https://kernel.googlesource.com/pub/scm/linux/kernel/git/firmware/linux-firmware.git/+/69e51bb26f3fa3e118dd770f1962a93bed690dc1/ath12k/WCN7850/hw2.0/)
下载 WCN7850 固件，按设备的 `firmware/wcn7850/manifest.json` 校验长度和 SHA-256。
已校验的文件通过源码缓存复用。测试包保留 `WCN7850-Notice.txt`。

工作流同时提前获取 Wi-Fi 组件锁定的 Linux、FreeBSD 和 supplicant 源码，
调用组件自带的校验器检查提交版本和源码包哈希。这些依赖加入有容量上限的源码缓存，
缓存按 Wi-Fi 组件版本区分，恢复后重新校验。

蓝牙锁定的 Linux 源码也在准备镜像前下载并校验哈希。前置检查同时核对 UART、
DMA、USB 及所选 ABI 输入文件。恢复的蓝牙源码按组件锁定记录重新校验。

下载系统镜像前，工作流检查运行环境和内核归档的构建脚本、固件打包文件、
内核参数长度及保留的 Data 种子。缺少输入或种子校验失败时提前结束。

工作流通过 VerifiedRoot 校验原始根镜像的每个块，生成签名策略，再构建内核、
固件与 payload。userdata 包含 OS、SystemOS、App 镜像窗口、策略哈希表，
以及清零的持久化存储与日志空间。

## 下载

在一天内下载 `sheng-macos-installation-seed` 和 `sheng-macos-test-<run-id>`。
保留 `data.dbr`，供后续构建使用。

在下载目录合并分卷并校验：

```sh
cat sheng-macos-test.tar.zst.part-* > sheng-macos-test.tar.zst
sha256sum -c sheng-macos-test.tar.zst.sha256
tar --zstd -xf sheng-macos-test.tar.zst
sha256sum -c SHA256SUMS
```

`boot.img` 用于临时启动。`userdata-full.img` 是覆盖整个 249497890816 字节
userdata 分区的 Android sparse 镜像，刷写会清空 Android 用户数据。
刷写前核对连接设备和分区大小。`test-package.json` 记录源码版本和镜像布局；
构建成功后仍需单独验证实机启动。

## 进度与复用

IPSW 提取每 10 秒显示已完成字节数、百分比、平均速度和预计剩余时间。
没有新数据时显示等待时长，HTTP 请求失败时显示重试次数和退避时间。

sheng 工作流使用 8 个并发请求，预取与当前 ZIP 成员相交的分块，元数据仍按顺序读取。
本地通过 `UNIXNU_IPSW_WORKERS` 设置 `1` 到 `8`，设为 `1` 时顺序读取，库默认值为 `4`。
预取范围受并发数和 8 MiB 分块大小限制。每个响应核对偏移和源文件大小，
仅重试失败分块，已校验的缓存分块继续供重启后复用。

后续运行复用最多 2 GiB 的 HTTP 下载分块、1 GiB 的源码缓存包和 2 GiB 的
编译缓存包。Data 种子单独缓存。下载分块按 URL、大小和 HTTP 版本标识匹配，
读取前校验哈希。达到限额后保留已缓存的分块，避免顺序读取挤掉下次构建需要的
数据；超出限额的部分重新下载。

源码缓存按来源版本匹配。编译缓存还核对跟踪文件、工作目录、Python、Xcode、
SDK、LLVM 和 Rust 版本。超限的缓存包跳过保存。构建失败时也保存下载缓存；
Data 种子缓存缺失时，仍需明确勾选 `init_data` 才能新建。

缓存从下次手动运行开始生效并积累，不改变已经运行的工作流。

构建日志实时显示运行环境、内核与固件的子进程输出。Mesa 配置和编译日志同步显示，
Ninja 显示已完成任务数、总任务数、百分比和耗时。运行环境脚本显示正在执行的命令。

连续 10 秒无输出时，显示当前日志名称、耗时、无输出时长、日志体积和磁盘剩余空间。
这表示子进程仍未退出，不代表编译有进展或已经卡死。完整原始日志仍保存在构建目录。

构建日志附件同时收集主流程及 Mesa、Metal、Wi-Fi 的内部日志，上传前隐藏平台种子。

## 磁盘空间

工作流清理 runner 中未使用的 Xcode 和模拟器，保留当前选中的 Xcode。
下载前要求至少 50 GiB 可用空间，不足时提前结束并显示剩余容量。

解密完成后删除对应的 AEA 下载副本，保留解密镜像和校验记录。
根镜像准备阶段生成布局与哈希报告，最终直接组装 sparse 镜像。
打包时流式压缩并分卷，避免生成完整压缩包后再复制分卷。

仅 Actions 在包内文件、版本记录和校验和全部写入后回收解密系统镜像。
本地收集保留这些镜像供重建使用。收集失败时保留源镜像和编译产物，
诊断磁盘预留空间仍会释放。
