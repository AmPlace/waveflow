<div align="center">

# 📻 WaveFlow
[![Demo](https://img.shields.io/badge/Demo-在线体验-blue?style=flat-square)](https://fm.bgm.gs) ![Stars](https://img.shields.io/github/stars/amplace/waveflow?style=flat-square) ![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=FastAPI&logoColor=white) ![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white) [![License](https://img.shields.io/github/license/amplace/waveflow)](./LICENSE)

**跨地区网络电台聚合网关与现代化 Web 播放器**

WaveFlow 是一个流媒体聚合平台，它通过后端的智能测速与代理，结合前端的响应式 UI，让你在一个页面内流畅收听并管理来自不同国家和地区的优质电台源
</div>



## ✨ 功能特性

- 📻 **台湾/大陆电台**： 可收听大陆及台湾的 1000+ 电台
- ⚡️ **自动路由**：多源冗余，每次发起播放时，后端会并发探测直连和代理线路的连通性，自动路由至最快可用源
- 🛡️ **跨域代理**：针对存在防盗链或 CORS 跨域限制的上游，后端实时接管并重写 m3u8 与 TS 请求，前端免受跨域困扰
- 🎨 **播放体验**： 支持锁屏/线控/车载控制、自适应深色模式
- 📺 **EPG 节目单**：解析云听等公开接口，电台卡片实时展示当前正在播送的节目名称
- 🚧 **可选区域限制**：通过 Cloudflare 请求头中的 IP 归属地，按需屏蔽特定电台
  
## 📸 界面预览

<div align="center">

  <img src="./.github/assets/pc.png" alt="PC 端播放界面" width="100%" />
  
  <br> 
  
  <img src="./.github/assets/phone-light.png" alt="手机端浅色模式" width="32%" />
  <img src="./.github/assets/phone-dark.png" alt="手机端深色模式" width="32%" />
</div>

## 📻 支持电台

| 数据源 | 覆盖地区 | 接入策略与说明 |
| :--- | :--- | :--- |
| **云听** | 中国大陆 | 支持**EPG 节目表** |
| **MyRadio** | 台湾 | 第三方聚合，含 90+ 台湾电台 |
| **自适配源** | 大陆/台湾 | 部分特殊电台 |
| **Radio Browser** | 全球 | 社区电台数据库，按国家/地区加载 |

## 🚀 快速开始

#### 🐳 Docker Compose（推荐）

项目已提供 `docker-compose.yml` 示例配置。

进入项目目录后，先选择一个由同一提交构建的不可变镜像标签，再启动：

```bash
export WAVEFLOW_RELEASE_VERSION=sha-<git-commit>
docker compose config
docker compose up -d
```

服务启动后：

- 前端默认发布在 `8080` 端口
- 后端只在 Compose 网络中暴露给前端，不直接发布到宿主机
- backend 与 frontend 使用同一个 `WAVEFLOW_RELEASE_VERSION`

将 `.env.example` 复制为 `.env` 后填写部署模式、Cookie 和必要的环境配置；不要把真实 secret 写入镜像或提交到仓库。

---

#### 🐳 Docker Run（可选）

如果你不使用 Docker Compose，也可以手动启动容器。

<details>
<summary>展开 Docker Run 部署命令</summary>

#### 1. 创建网络

```bash
docker network create waveflow-net
```

#### 2. 启动后端

```bash
docker run -d \
  --name waveflow-backend \
  --network waveflow-net \
  -v waveflow-data:/app/data \
  -e HITFM_COOKIE="" \
  --restart unless-stopped \
  ghcr.io/amplace/waveflow-backend:${WAVEFLOW_RELEASE_VERSION:?set WAVEFLOW_RELEASE_VERSION}
```

#### 3. 启动前端

```bash
docker run -d \
  --name waveflow-frontend \
  --network waveflow-net \
  -p 80:80 \
  --restart unless-stopped \
  ghcr.io/amplace/waveflow-frontend:${WAVEFLOW_RELEASE_VERSION:?set WAVEFLOW_RELEASE_VERSION}
```

</details>

---

## 💻 源码运行


#### 1. 后端

```bash
git clone https://github.com/amplace/waveflow.git

cd waveflow/backend

pip install -r requirements.txt

uvicorn main:app --reload --port 8000
```

#### 2. 前端

```bash
cd ../frontend

npm install

npm run dev
```

> 💡 提示：开发模式下，Vite 已配置本地 Proxy，会自动将前端的 `/api` 请求跨域转发至后端的 `localhost:8000`。

## ⚙️ 环境变量


| 变量名 | 说明 | 默认值 |
|---|---|---|
| `HITFM_COOKIE` | Hit FM 官网 Cookie（可选填写） | 空 |

---

## 🛠️ 技术栈

**前端**：Vue 3 · Pinia · Tailwind CSS · Vite · hls.js  
**后端**：Python 3.10+ · FastAPI · httpx · uvicorn

---

## 🗓️ TODO

- [ ] PWA 支持：可安装桌面/移动端
- [ ] 配置分享：订阅源打包为分享码或链接，支持导入他人配置
- [ ] IPTV 支持： 导入 IPTV 订阅、频道去重、测速，支持前端播放 IPTV ，显示 EPG

  
---

## 📄 License

本项目基于 [AGPL-3.0](./LICENSE) 协议开源。

---

## ⚠️ 免责声明

本项目仅供编程学习与技术交流使用。

项目代码不包含、不托管任何音频流媒体文件，所有电台数据及播放链接均通过网络公开接口聚合抓取。

使用者在使用过程中请遵守所在国家及地区的法律法规。对于因非法收听、录制、传播未经授权内容而引起的任何法律责任，由使用者自行承担，与本项目及开发者无关。
