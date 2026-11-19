# Compose Volume Check

[English](README.md)

在更新 Docker Compose 配置前，检查现有服务是否会换掉挂载的数据卷或宿主机目录。例如，搬动部署目录可能改变 Compose 项目名，进而使 PostgreSQL 挂载一个同样在配置里叫 `db-data`、实际名称却不同的卷。

需要 Python 3.10 或更新版本，以及可运行 `docker compose config --format json` 的 Compose。检查器使用 Python 标准库离线读取文件或管道，运行时无需 Docker 守护进程、管理员权限或第三方 Python 包。

## 在部署前检查

在准备变更前，从**当前部署使用的配置**建立快照。把下面的脚本和快照路径换成你本地的路径。

```sh
docker compose config --format json | python3 /path/to/compose_volume_check.py snapshot -o /path/to/baseline.json
```

准备好新版配置后，使用候选部署的实际参数执行检查。

```sh
docker compose config --format json | python3 /path/to/compose_volume_check.py check /path/to/baseline.json
```

如果部署时使用了 `-f`、`-p`、`--profile` 或 `--env-file`，渲染配置时使用相应版本的同一组参数。Windows 可在 PowerShell 7 中用 `py -3` 或已安装的 `python` 代替 `python3`。已有两份渲染后的 JSON 时，可直接运行 `python3 compose_volume_check.py check before.json after.json`。加 `--format json` 可供 CI 读取。

| 退出码 | 含义 |
| --- | --- |
| `0` | 所检查的现有服务没有发现配置层面的数据挂载变化，或基准没有数据挂载 |
| `1` | 有变化或无法确认的挂载，部署前请复核 |
| `2` | 输入或命令出错，检查未完成 |

## 复现项目改名导致的换卷

在工具目录运行下面两条命令。示例只渲染配置，适用于尚未运行任何容器的机器。

```sh
docker compose -p old -f examples/compose.yaml config --format json | python3 compose_volume_check.py snapshot -o baseline.json
docker compose -p new -f examples/compose.yaml config --format json | python3 compose_volume_check.py check baseline.json
```

预期退出码为 `1`，报告类似：

```text
[mount_changed] service "db", target "/var/lib/postgresql/data": The configured source changes from Docker volume "old_db-data" to Docker volume "new_db-data".
```

工具按服务名和容器内挂载目标对应两份配置。只改卷的 YAML 键而保留实际卷名时，工具识别为同一个卷。更换容器内挂载目标时，报告会标出原挂载位置缺失。带 profile 的服务缺席时，报告会提示核对启用的 profiles。

## 检查已知的服务改名

如果上游把服务从 `db` 改为 `postgres`，在检查时声明这次改名：

```sh
docker compose config --format json | python3 compose_volume_check.py check baseline.json --rename-service db=postgres
```

工具会按新服务名查找原挂载目标，并照常检查卷名、卷内子目录和驱动。多个服务改名时可重复传入该参数。每个旧名必须只出现在基准，每个新名只出现在候选，两端一对一对应。无效映射返回 `2`，卷变更和无法核实的挂载仍返回 `1`。JSON 报告用 `service_renames` 记录显式映射，发现项中的 `service` 沿用基准名称。

## 数据和判断范围

- 快照和报告仅保留与挂载判断有关的字段。真实 Docker 卷名、服务名和容器内挂载目标可能出现在输出中。宿主机绑定源、卷内子目录和卷驱动选项在快照里以 SHA-256 摘要保存，报告里省略原值。常见原值仍可能通过猜测和比对摘要推得，请把快照当作内部文件保管。
- 输入以 8 MiB 为限，重复键和过深嵌套会导致检查失败。终端控制字符会以 JSON 转义形式显示。Compose 渲染后的原始 JSON 可能含凭据，优先将它直接通过管道交给检查器。
- 结果表示配置意图。实际挂载取决于目标 Docker 环境、现存容器和卷、镜像以及卷驱动。匿名卷、`volumes_from`、无法识别的挂载、卷驱动选项变化和外部管理状态变化需要人工复核。工具只比较同一 Docker 环境下的两份配置，卷内容、外部卷是否存在、备份可恢复性和镜像内部数据目录变化需要独立核对。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

安装 Docker Compose 后，可以运行真实渲染测试。测试使用仓库里的合成配置，并将守护进程地址指向本机不可用的端口。它们只运行 `docker compose config`，因此无需启动 Docker 守护进程。

```sh
COMPOSE_VOLUME_CHECK_TEST_COMPOSE=1 python3 -m unittest discover -s tests -p test_rendered_compose.py -v
```

PowerShell 7 中可先执行 `$env:COMPOSE_VOLUME_CHECK_TEST_COMPOSE = '1'`，再运行同一条 Python 命令。可选的 `COMPOSE_VOLUME_CHECK_COMPOSE_BIN` 指向独立的 Compose 可执行文件，用于复核特定版本。仓库中的 GitHub Actions 工作流覆盖 Python 3.10、3.12 和 Docker Compose 2.38.2、5.7.0。

使用 [MIT 许可证](LICENSE)。
