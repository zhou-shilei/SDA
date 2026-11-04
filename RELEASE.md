# 开发商体验版 · 0.1.0-preview.1

发布分支：`release/developer-preview`。开发分支继续在 `feat/reader-relation-index` 维护；体验版使用独立工作目录、数据库和 4183 端口。

## 启动

已有 SDA 环境时：

```bash
SDA_STUDIO_PYTHON=/path/to/sda/bin/python bash studio/studio.sh start
```

打开 `http://127.0.0.1:4183/studio/?view=cases`。远程 VS Code 转发 **4183** 端口后打开；code-server 可使用 `/proxy/4183/studio/?view=cases`。

新环境先安装 `python -m pip install -r requirements-preview.txt`。内置案例的浏览、原页证据、审阅、数据集筛选及 CSV 导出可直接使用。上传与解析入口完整保留；执行新 PDF 解析还需要按 `environment.yml` 准备 GPU/OCR 环境和本机模型权重，并配置解析账号。网页姓名窗口可选择本人的登录文件；已有服务账号时可直接使用服务默认配置。账号与工作数据位于本机运行目录，未随案例包交付。

```bash
python scripts/check_release.py
python scripts/check_release.py --parsing
bash studio/studio.sh status
bash studio/studio.sh logs
```

`--parsing` 检查运行依赖、模型目录和客户端是否存在；实际账号权限及整篇解析仍由提交任务时的真实运行验证。

## 建议体验顺序

1. 在“体验案例”选择入门案例，点击“进入案例审阅”。
2. 点击 VH、VL 或实验数值，核对原文证据和缺失状态。
3. 在“任务队列”填写姓名并领取案例，尝试修正、标记和提交复核；审阅草稿只影响当前体验实例。
4. 在“数据集 → 体验案例”筛选已准备的记录并导出 CSV。
5. 在“数据录入”上传自己的 PDF，查看排队、解析状态及最终审阅结果。

| 案例 | 原文页数 | 记录数 | 体验重点 |
| --- | ---: | ---: | --- |
| US20150183856A1 | 31 | 38 | 小型案例、序列和实验数值 |
| WO2026086808A1 | 63 | 178 | 抗 TSLP 抗体、VH/VL 和实验标签 |
| WO2025108394A1 | 40 | 101 | 序列、实验和来源联查 |
| WO2024020591A9 | 263 | 253 | 长篇原文、多表证据和待复核项 |

共 570 条记录。案例来自已有真实提取结果；缺失、冲突和自动核验状态均保留，内置记录不预置人工通过决定。包内包含原始专利 PDF、表格裁图、精简后的界面数据及文件 SHA-256 清单。浏览案例不会重新运行 OCR 或调用模型。新建运行仍使用完整解析流水线。

首次启动为独立实例初始化“体验案例”数据集；后续启动保留使用者的修改和删除结果。内置原文案例有删除保护，上传的文档仍按原流程管理。数据库、任务队列、审阅人和凭据不会从开发环境复制过来。

## 分支维护

开发环境和体验环境并行运行。体验分支的修复单独提交，确认后再按需要合并开发改动。升级前备份体验实例的 `.studio/`；重启 HTTP 服务不会清空数据，也不会停止已提交的解析任务。

案例重建工具是 `scripts/prepare_experience.py --source-root /path/to/development --output /new/case-package`。它读取已有结果，不触发解析或模型请求，并拒绝覆盖已存在的输出目录。
