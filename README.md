# Pipplework — 3D 数据采集 & 清洗管线

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Three.js-r168-black?logo=three.js" alt="Three.js">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
</p>

全自动 3D 模型数据管线：从公开模型平台（Printables / Thingiverse）**爬取 → 清洗 → 转换 → 展示**，支持实时 WebSocket 进度推送和浏览器内 GLB 3D 预览。

---

## 功能特性

- **双源爬虫** — 支持 Printables（GraphQL API）和 Thingiverse（REST API + 页面降级）
- **TLS 指纹伪装** — 基于 curl_cffi 的 Chrome TLS 指纹，绕过 Cloudflare 等 WAF
- **8 阶段数据清洗** — 文件大小 → 完整性 → 网格加载 → 结构验证 → 几何质量 → 复杂度 → 去重 → GLB 转换
- **实时可视化** — WebSocket 推送管道事件，前端动画流程图 + Three.js GLB 查看器
- **脏数据追踪** — 14 种拒绝原因（中英文），可视化统计面板

## 项目结构

```
├── backend/
│   ├── main.py              # FastAPI 主应用，REST + WebSocket API
│   ├── config.py            # 管线配置（阈值、路径、延迟等）
│   ├── models.py            # Pydantic 数据模型 & 枚举
│   ├── ws_manager.py        # WebSocket 连接管理 & 事件广播
│   ├── crawler/
│   │   ├── engine.py        # 爬虫基类（TLS 伪装、限速、重试）
│   │   ├── printables.py    # Printables GraphQL 爬虫
│   │   └── thingiverse.py   # Thingiverse API 爬虫
│   ├── cleaner/
│   │   └── pipeline.py      # 8 阶段清洗管线（trimesh）
│   └── storage/
│       └── db.py            # SQLite 异步数据库层
├── frontend/
│   ├── index.html           # 主页面
│   ├── css/style.css        # 样式（暗色主题）
│   └── js/
│       ├── app.js           # 主逻辑 & WebSocket
│       ├── viewer.js        # Three.js GLB 查看器
│       └── pipeline-viz.js  # 管道流程动画
├── setup.sh                 # 服务器一键初始化脚本
├── deploy.sh                # 快速更新部署脚本
├── nginx-pipplework.conf    # Nginx 反向代理配置
├── requirements.txt         # Python 依赖
└── .gitignore
```

## 技术栈

| 层      | 技术                                                     |
| ------- | -------------------------------------------------------- |
| 后端框架 | FastAPI + Uvicorn                                       |
| 爬虫引擎 | curl_cffi（Chrome TLS 指纹） + BeautifulSoup + GraphQL  |
| 3D 处理  | trimesh + NumPy + SciPy                                 |
| 数据库   | SQLite（aiosqlite 异步）                                |
| 前端渲染 | Three.js r168 + GLTFLoader + OrbitControls              |
| 通信     | WebSocket 实时事件推送                                   |

## 快速开始

### 1. 环境要求

- Linux 服务器（Debian 12 / Ubuntu 22.04+）
- Python 3.11+
- root 权限（用于 systemd 服务）

### 2. 一键初始化

```bash
# 克隆仓库
git clone https://github.com/ucarcompany/pipplework-deploy.git
cd pipplework-deploy

# 运行初始化脚本（安装依赖、创建虚拟环境、配置 systemd 服务）
sudo bash setup.sh
```

### 3. 复制代码到服务器

```bash
# 将项目文件复制到服务器目录
cp -r backend/* /opt/pipplework/backend/
cp -r frontend/* /opt/pipplework/frontend/

# 启动服务
sudo systemctl start pipplework
sudo systemctl enable pipplework
```

### 4. Nginx 反向代理（可选）

将 `nginx-pipplework.conf` 中的 location 块添加到你的 Nginx 站点配置中：

```bash
sudo cp nginx-pipplework.conf /etc/nginx/snippets/
# 然后在你的 server block 中 include /etc/nginx/snippets/nginx-pipplework.conf;
sudo nginx -t && sudo systemctl reload nginx
```

### 5. 访问

- 直连：`http://<服务器IP>:9800`
- 通过 Nginx 代理：`https://yourdomain.com/pipplework/`

## API 文档

| 方法    | 路径                                 | 说明           |
| ------- | ------------------------------------ | -------------- |
| `POST`  | `/api/crawl/start`                   | 启动爬取任务   |
| `GET`   | `/api/crawl/jobs`                    | 列出所有任务   |
| `GET`   | `/api/crawl/jobs/{job_id}`           | 查询单个任务   |
| `POST`  | `/api/crawl/stop/{job_id}`           | 停止任务       |
| `GET`   | `/api/models`                        | 已清洗模型列表 |
| `GET`   | `/api/models/{id}`                   | 模型详情       |
| `GET`   | `/api/models/{id}/file`              | 下载 GLB 文件  |
| `GET`   | `/api/dirty`                         | 脏数据列表     |
| `GET`   | `/api/stats`                         | 管线统计       |
| `GET`   | `/api/events`                        | 事件日志       |
| `WS`    | `/ws`                                | 实时事件推送   |

### 示例：启动爬取

```bash
curl -X POST http://localhost:9800/api/crawl/start \
  -H "Content-Type: application/json" \
  -d '{"query": "box", "max_models": 3}'
```

## 清洗管线

每个下载的 3D 文件经过 **8 个阶段** 的质量检测：

```
文件 → [1.大小检查] → [2.完整性校验] → [3.网格加载] → [4.结构验证]
     → [5.几何质量] → [6.复杂度检查] → [7.内容去重] → [8.GLB转换] → ✅ 清洗数据
                                                                   → ❌ 脏数据 + 原因
```

| 阶段 | 检查项         | 阈值                     |
| ---- | -------------- | ------------------------ |
| 1    | 文件大小       | 100 B — 100 MB           |
| 2    | Magic Bytes    | STL/GLB/3MF/PLY 签名验证 |
| 3    | 网格加载       | trimesh 能否解析         |
| 4    | 基础结构       | ≥ 4 顶点, > 0 面片      |
| 5    | 退化面片比例   | < 30%                    |
| 5    | NaN 法线比例   | < 10%                    |
| 6    | 面片数范围     | 10 — 2,000,000           |
| 7    | SHA-256 去重   | 顶点+面片字节哈希        |
| 8    | GLB 导出       | trimesh → GLB 二进制     |

不通过的文件会被标记为 14 种 `DirtyReason` 之一，并归档到 `rejected/` 目录。

## 反爬策略

| 手段               | 实现方式                                       |
| ------------------ | ---------------------------------------------- |
| TLS 指纹伪装       | curl_cffi 模拟 Chrome 124 的 JA3/JA4 指纹     |
| User-Agent 轮换    | 4 组真实浏览器 UA 随机切换                     |
| 请求限速           | 8–20s 随机延迟（可配置）                       |
| 指纹重建           | 遇到 403 时自动更换 TLS 指纹和 UA              |
| GraphQL 伪装       | 完整的浏览器请求头（Origin, Sec-Fetch-* 等）   |

## 配置

核心配置位于 `backend/config.py`，可通过环境变量 `PIPELINE_BASE` 修改基础路径：

```python
CRAWL_DELAY_RANGE = (8, 20)       # 请求间隔（秒）
MAX_FILE_SIZE = 100 * 1024 * 1024 # 最大文件 100MB
MIN_FACE_COUNT = 10               # 最少面片数
MAX_FACE_COUNT = 2_000_000        # 最多面片数
MAX_DEGENERATE_RATIO = 0.3        # 退化面片比例上限
```

## 开发

```bash
# 本地开发（需要安装依赖）
pip install -r requirements.txt

# 直接运行
uvicorn backend.main:app --host 0.0.0.0 --port 9800 --reload
```

## License

MIT
