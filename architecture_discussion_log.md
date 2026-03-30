# 总体架构与选型讨论记录 (Conversation & Design Decisions Log)

## 需求背景
依据 `requirement.txt`，目标是在 Windows 11 WSL-Ubuntu Host 上安全运行 openclaw (openclaw.ai) 的 Docker 环境，且要求：
1. 文件与网络出站访问统一由 Host 管理。
2. AI 模型请求经过 Host 拦截和安全审查。
3. 必须由一个 Python CLI 程序进行统一的调度编排。
4. 论证引入 NVIDIA OpenShell 的可行性。

## 架构演进核心 Q&A

### 提问 1：原计划架构与 OpenShell 的能力是否有重叠？
**架构师解答**：是的，在“边界隔离”上有明显重叠。
我们在定制 Python 代理和挂载映射时，所试图解决的文件沙箱、出站网络拦截，恰恰是 OpenShell 核心 Gateway 的原始本职工作。不过在“LLM 请求的自然语言语义安全性拦截”方面，OpenShell 提供底层的路由抓取，而我们需要补充顶层的 FastAPI Reviewer 进行意图拦截，二者互补。

### 提问 2：关于采用 OpenShell 的系统决定
**初期判断与反转**：
初期因为担心双重套接沙箱（Docker内嵌套运行拦截限制）会导致代码可维护性崩塌（正常程序流派容易被拦截误杀），一度建议弃用 OpenShell。
但在详细阅读 NVIDIA / OpenShell 项目 GitHub 源码库和说明后，确认 **OpenShell 是针对此类 Agent 的最佳编排底座**。它原生兼容并支持了代理 OpenClaw，能原生解决大模型凭据隔离（Inference Routing）等复杂的开发需求。最终决定**全面拥抱 OpenShell** 代替我们自己从零手写的 Docker 控制器。

### 提问 3：如果使用 OpenShell，为什么拦截网关不直接存在于 Host 宿主机？
**架构师解答（澄清边界）**：
OpenShell 的逻辑管控 CLI 确实安装在我们的 Windows WSL-Ubuntu 宿主机上，但这套系统为了极高的安全上限，将它的**控制与拦截核心 (Gateway引擎)** 给做了独立的 Docker 容器化（其内部跑着轻量化 K3s 服务）。
*   它的物理数据流形式是：`[OpenClaw Agent 容器]` ---> `[OpenShell Gateway 容器]` ---> `[Host 网络层]` ---> `[互联网/外部模型]`。
*   **设计收益**：即使底层 Gateway 配置出现严重失误或遭受污染，因为它是从物理层面被包裹在 Docker 专有容器里的，所以绝不会渗透波及 Windows(WSL) 主操作系统的内核或其他网络进程。这种隔离理念极为干净、严苛。

## 最终定稿的技术选型
*   **安全沙箱引擎**：NVIDIA OpenShell Native Gateway
*   **编排管理台**：Python CLI (`typer` 库) — 负责自动化调用 OpenShell 指令与生成管控 YAML 策略。
*   **LLM 意图审查器**：Python API (`fastapi` 库) — 挂载于本地，承接 OpenShell 拦截抛出的大模型请求，执行词汇级与模式匹配审查，合规则放行。
