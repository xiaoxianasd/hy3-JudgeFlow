# 安全沙盒设计与运行说明

## 1. 目标与边界

TraceJudge 会执行 Hy3 生成的 Python 代码，因此所有候选代码都按不可信代码处理。沙盒需要限制网络、文件、进程、CPU、内存、运行时间和输出体积，并保证一次任务失败不会影响 API、Worker 或其他任务。

安全模型分为两级：

| 后端 | 用途 | 是否可用于公网生产 |
|---|---|---|
| `local` | Windows 开发、单元测试、离线调试 | 否 |
| `docker` | Linux/容器环境中的一次性隔离执行 | 是，仍需正确保护 Docker daemon |

`local` 后端沿用独立 Python 进程，同时增加 AST 策略、受限 builtin/模块、输入输出上限和强制超时。它能够拦截常见危险代码，但无法成为抵御主动攻击者的宿主机安全边界。

## 2. 执行链

```text
Evaluator / Hypothesis
  → SandboxExecutor
      → 输入与配置上限校验
      → LocalSandbox 或 DockerSandbox
          → 独立 runner.py
              → AST 安全策略
              → 受限 builtins / module facade
              → 候选函数执行
              → 有界 JSON 结果
```

固定测试、隐藏测试、参考实现和 Hypothesis 动态差分统一通过该接口，不存在绕开沙盒的第二条代码执行路径。

## 3. Docker 镜像

开发构建：

```bash
docker build -f sandbox/Dockerfile -t hy3-process-sandbox:py3.12 .
```

沙盒运行器或安全白名单更新后必须重新执行上述构建并重启 Web/Worker；已有同名镜像不会自动包含代码更新。

正式发布应把基础镜像固定到审核过的 digest：

```bash
docker build \
  --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<verified-digest> \
  -f sandbox/Dockerfile \
  -t hy3-process-sandbox:release .
```

镜像只包含 Python 与独立 Runner，使用 UID/GID `65532`，没有项目源码、数据集、Hy3 Key 或宿主目录。

## 4. Docker 运行限制

每次执行生成一个随机容器名，并使用以下等价参数：

```text
--rm
--pull never
--network none
--ipc none
--read-only
--memory 256m
--memory-swap 256m
--cpus 1.0
--pids-limit 32
--cap-drop ALL
--security-opt no-new-privileges:true
--user 65532:65532
--ulimit nofile=64:64
--ulimit core=0:0
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m
--stop-timeout 1
```

容器不使用 volume/bind mount。父进程超时后会用经过格式校验的精确容器名执行 `docker rm -f`，防止只杀死 Docker CLI 而遗留后台容器。

## 5. Runner 策略

Runner 在容器内部再次执行防御：

- 只允许 `bisect`、`collections`、`functools`、`heapq`、`math` 中明确列出的安全成员；支持 `import module` 和 `from module import member`，拒绝星号、相对、子模块及其他导入；
- 禁止 class、async/await；
- 禁止双下划线和私有属性访问；
- 禁止 `open/eval/exec/compile/getattr/globals/vars` 等名称；
- AST 节点数量最多 5000；
- 代码默认最多 64 KiB；
- 单个返回值默认最多 64 KiB；
- 总输入默认最多 2 MiB；
- 总输出默认最多 1 MiB；
- 测试数量默认最多 512；
- Linux 中额外设置 address-space、CPU、文件大小和文件描述符 rlimit。

允许的标准能力由 `SAFE_BUILTINS` 和只读 module facade 明确列出，包括常用容器、数学、堆、二分和有限缓存函数。

AST/关键词规则只是纵深防御，真正的安全边界仍然是受限容器。

## 6. 环境配置

本地开发：

```dotenv
APP_ENV=development
SANDBOX_BACKEND=local
ALLOW_UNSAFE_LOCAL_EXECUTION=true
```

生产环境：

```dotenv
APP_ENV=production
SANDBOX_BACKEND=docker
ALLOW_UNSAFE_LOCAL_EXECUTION=false
SANDBOX_DOCKER_IMAGE=hy3-process-sandbox:release
SANDBOX_MEMORY_MB=256
SANDBOX_CPUS=1.0
SANDBOX_PIDS_LIMIT=32
SANDBOX_MAX_INPUT_BYTES=2097152
SANDBOX_MAX_OUTPUT_BYTES=1048576
SANDBOX_MAX_CODE_BYTES=65536
SANDBOX_MAX_TESTS=512
SANDBOX_MAX_VALUE_BYTES=65536
```

当 `APP_ENV=production` 时，即使误配 `ALLOW_UNSAFE_LOCAL_EXECUTION=true`，本地后端仍会返回 `UnsafeLocalSandboxDisabled`。

## 7. Docker daemon 安全

不要把 Docker TCP API 暴露到公网，也不要把无保护的 `/var/run/docker.sock` 挂载到公开 API 容器。Docker socket 通常等价于宿主机高权限。

推荐让专用 Evaluation Worker 在隔离的 Linux 主机或 VM 上管理沙盒容器：

```text
Public API → Task Queue → Dedicated Sandbox Worker Host → Disposable Containers
```

Worker 自身应使用最小权限账户，并限制只能启动指定镜像。更高安全级别可进一步迁移到 rootless Docker、containerd + gVisor、Kata Containers 或独立微型虚拟机。

## 8. 验收

不需要 Docker 的策略测试：

```bash
python -m unittest tests.test_sandbox -v
```

完整回归：

```bash
python -m unittest discover -s tests -v
```

Docker 可用时，应额外验证：

1. 构建固定版本镜像；
2. 设置 `SANDBOX_BACKEND=docker`；
3. 运行全部 66 道参考实现；
4. 验证无限循环、内存分配、文件读取、网络访问、子进程、反射和输出炸弹；
5. 检查每次执行后没有残留 `hy3-sandbox-*` 容器；
6. 并发执行后确认 Worker、Docker daemon 和宿主机稳定。

当前开发机器未安装 Docker，因此仓库内已验证 Runner 策略和 Docker 命令参数，但真实容器攻击测试必须在安装 Docker 后完成，不能用模拟测试代替。
