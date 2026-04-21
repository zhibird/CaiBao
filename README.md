## 项目预览

<p align="center">
  <img src="./caibao_pic.png" alt="CaiBao 项目预览" width="720">
</p>

## 快速开始

### 本地 Python 运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Windows PowerShell 可将激活命令替换为：

```powershell
.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

启动后可访问：

- 前端首页：`http://localhost:8000/`
- 健康检查：`http://localhost:8000/api/v1/health`

## Docker 运行

### 1. 准备环境变量

```bash
cp .env.example .env
```

至少建议修改以下配置后再启动：

- `AUTH_JWT_SECRET`
- `POSTGRES_PASSWORD`
- `LLM_API_KEY`（如果你要接真实模型）

### 2. 启动服务

```bash
docker compose up --build -d
```

默认会启动两项服务：

- `postgres`：PostgreSQL 16
- `caibao-api`：FastAPI + Alembic 自动迁移

### 3. 常用命令

```bash
docker compose ps
docker compose logs -f caibao-api
docker compose down
```

如需连同数据库卷一起清理：

```bash
docker compose down -v
```

当前 Docker 镜像已经补齐：

- PostgreSQL 运行环境
- Alembic 启动迁移
- `/data` 持久化卷
- 容器健康检查
- 非 root 用户运行
- `tesseract-ocr` + `tesseract-ocr-chi-sim`，支持容器内中文 OCR

## changelogs

- **v0.1.0**：后端 MVP + 前端基础页面
- **v0.2.0**：会话管理（引入会话 `conversation_id`，实现会话隔离、删除）
- **v0.3.0**：支持会话重命名、消息删除、用户发送的对话消息重新编辑
- **v0.4.0**：完善调用大模型 API 对话能力，支持用户在前端填写 API Key / Base URL 配置模型，并按账号隔离使用
- **v0.5.0**：优化前端界面，新增会话置顶（pin）接口，支持复制与切换模型重生成
  - **v0.5.1**：前端 UI 细节优化
- **v0.6.0**：
  1）AG 检索链路正式支持真实 embedding（非 mock）并可安全重建索引
  2）增真实场景性能评估数据集
- **v0.7.0**：
  1）面向团队暂时调整为面向用户（account），产品初步定位为小豆包
  2）答返回补齐 `mode + sources`，支持前端清晰展示回答模式与来源
  3）页新增场景卡区块，点击后可一键填充提示词模板
  - **v0.7.1**：修复 RAG 链路存在的 bug
- **v0.8.0**：将会话内 RAG 交互从“系统视角的文档范围配置”调整为“用户视角的附件式聊天流”，更符合豆包心智模型
- **v0.9.0**：新增“开发者可用”的管理员账户与独立管理后台，集中管理团队/用户/会话/上传文件
  - **v0.9.1**：隔离测试用数据库，修复测试数据污染主库问题，清理历史脏数据
- **v0.10.0**：新增支持 PDF 与图片上传与解析功能
  - **v0.10.1**：修复附件预览时报错的问题
  - **v0.10.2**：支持将文件拖拽进聊天输入区完成附件上传
  - **v0.10.3**：修正图片附件的调用逻辑（支持多模态）
  - **v0.10.4**：修复模型回复显示不完整的问题
- **v0.11.0**：支持模型输出图片
  - **v0.11.1**：修复模型图片输出的续写污染与远程图片历史失效问题
  - **v0.11.2**：支持点击预览聊天图片，并在预览界面下载图片
- **v0.12.0**：附件上传新增支持 Word / Excel（`docx` / `xlsx`）
  - **v0.12.1**：修复图片附件问答在部分 OpenAI-compatible 提供方返回 400 的问题
  - **v0.12.2**：升级前端UI，优化交互体验
- **v0.13.0**：支持同会话上下文记忆
- **v0.14.0**：新增回答收藏（favorites）与历史结论（conclusions）沉淀能力，支持从收藏转记忆卡/结论
  - **v0.14.1**：定位与链路校正版本，默认直聊、资料按需增强，并补齐后端语义与回归测试
  - **v0.14.2**：回答卡片接入“收藏 / 记忆 / 结论 / 资料库”沉淀闭环，补齐来源追溯、状态回显与前端缓存刷新
  - **v0.14.3**：新增收藏夹工作区，支持收藏后的承接整理，并允许再次点击“已收藏”取消收藏；同时为记忆 / 结论 / 资料库下沉动作补齐去重保护
  - **v0.14.4**：隐藏Admin入口，修复409冲突问题
- **v0.15.0**：升级技术栈，数据库从SQLite升级至postgreSQL
- **v0.16.0**：实现登录鉴权闭环
- **v0.17.0**：前端究极大更新！
