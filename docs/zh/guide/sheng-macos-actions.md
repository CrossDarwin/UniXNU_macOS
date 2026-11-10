# 用 Actions 构建 sheng macOS 测试包

`sheng macOS test package` 工作流为 256 GB sheng 构建 macOS 27.3（`26C5035r`）。
工作流使用 `xcode-27` ARM64 macOS runner，设备与 UEFI 仓库固定到指定提交。

## 构建

在仓库 Actions Secrets 中设置 `UNIXNU_UNIQUE_CHIP_ID`，值为本次安装使用的
8 位十六进制平台种子。后续构建保留该值。

在 `sheng/macos-actions` 分支手动运行工作流。首次安装且没有缓存的 Data 种子时，
勾选 `init_data`。后续构建复用种子。推送提交不触发构建。

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

后续运行复用最多 2 GiB 的 HTTP 下载分块、1 GiB 的源码缓存包和 2 GiB 的
编译缓存包。Data 种子单独缓存。下载分块按 URL、大小和 HTTP 版本标识匹配，
读取前校验哈希。达到限额后保留已缓存的分块，避免顺序读取挤掉下次构建需要的
数据；超出限额的部分重新下载。

源码缓存按来源版本匹配。编译缓存还核对跟踪文件、工作目录、Python、Xcode、
SDK、LLVM 和 Rust 版本。超限的缓存包跳过保存。构建失败时也保存下载缓存；
Data 种子缓存缺失时，仍需明确勾选 `init_data` 才能新建。

缓存从下次手动运行开始生效并积累，不改变已经运行的工作流。

## 磁盘空间

工作流清理 runner 中未使用的 Xcode 和模拟器，保留当前选中的 Xcode。
下载前要求至少 50 GiB 可用空间，不足时提前结束并显示剩余容量。

解密完成后删除对应的 AEA 下载副本，保留解密镜像和校验记录。
根镜像准备阶段生成布局与哈希报告，最终直接组装 sparse 镜像。
打包时流式压缩并分卷，避免生成完整压缩包后再复制分卷。
