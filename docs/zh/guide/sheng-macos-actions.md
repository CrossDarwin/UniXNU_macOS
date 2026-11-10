# 用 Actions 构建 sheng macOS 测试包

`sheng macOS test package` 工作流为 256 GB sheng 构建 macOS 27.3（`26C5035r`）。
工作流使用 `xcode-27` ARM64 macOS runner，设备与 UEFI 仓库固定到指定提交。

## 构建

在仓库 Actions Secrets 中设置 `UNIXNU_UNIQUE_CHIP_ID`，值为本次安装使用的
8 位十六进制平台种子。后续构建保留该值。

在 `sheng/macos-actions` 分支运行工作流。首次安装且没有缓存的 Data 种子时，
勾选 `init_data`。后续构建复用种子。该分支的首次推送也允许创建种子。

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
