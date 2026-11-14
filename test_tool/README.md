# 测试与诊断工具

`test_tool/` 存放主机测试、代码审计、QEMU 探针和日志分析工具。构建、制盘和运行时脚本位于 `scripts/`，组件自身的测试保留在对应组件仓库中。

从仓库根目录执行：

```sh
python3 test_tool/check-unixnu.py
python3 -m unittest discover -s test_tool/ci -p 'test_*.py'
python3 -m unittest discover -s test_tool/unixnu -p 'test_*.py'
python3 -m unittest discover -s test_tool/qemu-entry -p 'test*.py'
python3 -m unittest discover -s test_tool/terminal -p 'test*.py'
python3 test_tool/sheng-apfs/test-capacity.py
python3 test_tool/sheng-apfs/test-package-default.py
```

`sheng-apfs/` 提供容器布局、扩容、内存回收和持久日志验证。`qemu-entry/` 提供启动夹具、GDB 连接和运行时取证工具。运行探针前，按对应脚本参数准备内核、系统镜像和工具链。
