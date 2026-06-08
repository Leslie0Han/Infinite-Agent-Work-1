# Infinite Agent Work

AI 驱动的无限画布工作台，面向图片生成、参考图组织、资源沉淀和多模型协作。

当前版本已经不是单一的生图页，主线工作流是：

1. 在资源库整理素材
2. 把图片送进智能画布做参考、编辑、生成
3. 将结果回存资源库，继续标注、筛选、导出

## 主要功能

### 1. 画布系统

- `无限画布`：通用拖拽、连线、分组、多画布管理
- `智能画布`：面向图片/视频生成的轻工作流台
- 支持普通画布与智能画布分开创建、保存、恢复

### 2. 智能画布

- 从资源库插入图片
- 多图参考、图片分组、连线表达依赖关系
- API 生图、ModelScope、生视频、ComfyUI 工作流
- 裁剪、蒙版、涂抹、对比预览
- 导出当前画布引用素材
- 将选中节点结果回存资源库

### 3. 资源库

- 管理本地素材来源
- 扫描文件夹并生成缩略图
- 分类、手动标签、AI 标签、收藏、备注
- 从资源库一键新建智能画布
- 对从智能画布回存的图片，可直接回到来源画布

### 4. 模型与服务

- 在线 API：支持 OpenAI 兼容协议、APIMart 异步协议
- ModelScope：支持图片生成和 LoRA
- ComfyUI：支持多个后端、工作流模板和自定义参数
- GPT / LLM：支持多轮对话和模型切换

## 页面入口

| 页面 | 路径 | 说明 |
| --- | --- | --- |
| 首页 | `/` | 主入口，包含各工作区标签页 |
| 画布管理 | `/static/canvas.html` | 普通画布和智能画布管理 |
| 智能画布 | `/static/smart-canvas.html?id=...` | 参考图、生成、编辑、导出 |
| 资源库 | `/static/library.html` | 素材来源、分类、标签、回流 |
| 在线生成 | `/static/online.html` | 在线 API 生图 |
| 图片增强 | `/static/enhance.html` | 放大与增强 |
| GPT 对话 | `/static/gpt-chat.html` | 多轮聊天 |
| API 设置 | `/static/api-settings.html` | 配置 API 提供商 |
| ComfyUI 设置 | `/static/comfyui-settings.html` | 配置本地或远程 ComfyUI |

## 启动方式

### 方式一：源码启动

适合开发和调试。

```bash
git clone https://github.com/Leslie0Han/Infinite-Agent-Work.git
cd Infinite-Agent-Work
pip install -r requirements.txt
python main.py
```

默认地址：

- 本机访问：[http://127.0.0.1:3000/](http://127.0.0.1:3000/)
- 也可以用：[http://localhost:3000/](http://localhost:3000/)

### 方式二：Windows 双击启动

项目根目录提供了：

- `启动服务.bat`

如果目录里已经带好了 `python/` 运行环境，通常直接双击就能启动，不需要额外装 Python。

### 方式三：macOS 启动

项目根目录提供了：

- `mac-启动服务.sh`
- `mac-安装依赖.sh`

如果你是从打包分发的压缩包直接拿到项目，第一次建议先安装依赖，再启动：

```bash
./mac-安装依赖.sh
./mac-启动服务.sh
```

## macOS 首次运行说明

如果你把启动脚本改成了 `.command` 或从外部分发给别人，首次运行 macOS 可能会拦截。

常见处理方式：

1. 右键启动脚本
2. 选择“打开”
3. 在系统弹窗里再次点击“打开”

如果仍然提示无法验证开发者：

1. 打开“系统设置”
2. 进入“隐私与安全性”
3. 在底部找到被拦截的脚本
4. 点击“仍要打开”

## 局域网访问

服务启动后，除了本机地址，还可以给同一局域网下的设备访问：

- `http://你的局域网IP:3000/`

适合在另一台电脑、平板或手机上预览页面。

## 首次配置建议

### 在线 API

进入 `/static/api-settings.html` 配置：

- 请求地址
- 协议类型
- API Key
- 图片 / 聊天 / 视频模型

如果使用 APIMart，协议要切到 `APIMart 异步`。  
如果使用其他兼容服务，一般选 `OpenAI 兼容`。

### ModelScope

如果想走免费或半本地链路，可以配置 ModelScope Token，并按需绑定 LoRA。

### ComfyUI

如果你要走本地工作流：

1. 启动本地或远程 ComfyUI
2. 在 `/static/comfyui-settings.html` 填入地址
3. 选择可用工作流或导入自定义工作流

## 推荐工作流

### 素材到生成

1. 在资源库添加本地来源并扫描
2. 给图片补分类、标签或 AI 标注
3. 在资源库打开某张图，点击“发送到智能画布”
4. 在智能画布里继续追加参考图、修改 prompt、运行生成
5. 选中结果节点，点击“存入资源库”
6. 需要分享或归档时，点击“导出素材”

### 结果回看

如果某张资源库图片本来是从智能画布回存的，详情抽屉里可以直接“回到来源画布”。

## 项目结构

```text
.
├── main.py
├── static/
│   ├── index.html
│   ├── canvas.html
│   ├── smart-canvas.html
│   ├── library.html
│   ├── api-settings.html
│   └── ...
├── workflows/
├── data/
│   ├── canvases/
│   └── library/
├── assets/
│   ├── input/
│   └── output/
├── requirements.txt
├── InfiniteAgentWork.spec
├── 启动服务.bat
├── mac-启动服务.sh
└── mac-安装依赖.sh
```

## 打包与分发经验

这部分是从旧版分发经验里保留下来的，比较实用：

- 优先保留双击启动脚本，降低非开发用户上手门槛
- 如果环境允许，可以随项目附带 `python/` 运行环境，避免用户手动装 Python
- 启动成功后尽量自动打开浏览器，并明确显示访问地址
- 文档里一定要写明 `localhost`、`127.0.0.1`、局域网地址三种入口
- macOS 分发一定要单独写首次放行说明，不然用户经常卡在系统拦截

## 常见问题

### 1. 端口 3000 被占用

先结束旧进程，再重新启动服务。

### 2. 双击脚本没有反应

- Windows：优先确认目录中是否有可用的 Python 运行环境
- macOS：先按上面的“首次运行说明”放行脚本

### 3. 提示缺少 Python

安装 Python 3.10+，或者直接使用项目目录里附带的 `python/` 环境。

### 4. 依赖安装失败

可以先升级 pip，再重装：

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5. 运行时报错但不确定怎么排查

优先看启动终端里的报错内容；如果是前端功能异常，打开浏览器控制台看请求和报错信息会更快。

## 开发说明

- 后端：`FastAPI`
- 前端：原生 HTML / CSS / JavaScript
- 打包：`PyInstaller`（见 `InfiniteAgentWork.spec`）
- 数据默认存本地 `data/` 和 `assets/`

## License

MIT
