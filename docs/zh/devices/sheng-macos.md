# sheng 上的 macOS

搭载 SM8550 的社区设备移植。当前状态依据下方固定版本的维护者记录。

| 仓库 | 维护者 |
| --- | --- |
| [macOS-ForSheng](https://github.com/Jelly10086/macOS-ForSheng) | [Jelly10086](https://github.com/Jelly10086) |

## 当前状态

配对的 macOS 26.5 / 25F71 内核与恢复用户态已在 QEMU 和 sheng 实机运行原始 arm64e `launchd`。实机记录覆盖 BaseSystem 与 ISC 挂载、临时 Data 和模板复制。日志可保存到 `macoslog` 并在重启后读回。

WindowServer 在 HID 和显示初始化期间反复退出，图形 Recovery 待完成。配对输入和设备配置按当前状态记录选择。

## 来源

- [状态: Jelly10086/macOS-ForSheng](https://github.com/Jelly10086/macOS-ForSheng/blob/4ec9620f127fe0daa7e9e9498ee7b0d374fd032f/docs/status.md)

## 使用此移植

1. 阅读上方仓库的构建说明和固定版本记录，准备对应的固件、系统镜像和板级输入。
2. 按该仓库的命令构建，通用配置见[构建与启动](/zh/building)。
3. 按[启动验证顺序](/zh/porting/bring-up)逐项验证，通过[日志抓取](/zh/porting/log-capture)保存结果。
4. 更新[移植记录](/zh/porting/records)，记录实际使用的源码版本及最后确认的阶段。

Native APFS 镜像需要为首次启动和 swap 保留容器空间。[制盘脚本](https://github.com/CrossDarwin/UniXNU_macOS/blob/test_27/scripts/sheng-apfs/README.md#grow-an-existing-container)为新镜像分配 48 GiB。首次启动自动扩展到 userdata 容量减去末尾 1 GiB 日志区，Data 和 VM 共享容器余量。扩容日志位于 `/var/log/unixnu-apfs-grow.log`。使用 `vm_stat` 和 `sysctl vm.swapusage` 分别核对内存压缩和磁盘交换。
