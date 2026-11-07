# 小浣熊量化选股 (RaccoonQuant)

A 股量化选股桌面应用，基于 PyQt6 构建，提供早盘竞价、尾盘分析、首板推荐、每日超跌科技股、高分红价值股等多套选股策略，并集成实时行情、板块概念、资金流向与财经快讯。

> 仅供学习与研究使用，**不构成任何投资建议**。股市有风险，入市需谨慎。

---

## ✨ 功能特性

| 模块 | 说明 |
| --- | --- |
| 早盘竞价 | 09:25 集合竞价后筛选竞价异动、量比放大、流通市值适中的潜力标的 |
| 尾盘分析 | 14:30 尾盘时段结合实时涨幅、量比、换手进行复盘选股 |
| 首板推荐 | 涨停首板（非连板）标的挖掘 |
| 每日推荐 | 基本面良好且持续下跌的科技股超跌发现 |
| 分红选股 | 高股息、财报正增长 + 现金流健康的价值股筛选 |
| 股票搜索 | 支持代码 / 名称 / 拼音首字母模糊搜索，并展示实时行情、概念、资金流向、涨跌幅 |
| 财经快讯 | 实时财经电报（财联社 / 新浪 7x24 兜底） |

- 多数据源聚合：腾讯 `gtimg`、东方财富、同花顺、通达信（TDX）、雪球，腾讯行情为主、东财等多源兜底。
- 本地缓存 + 可选 MySQL 持久化，减少重复请求。
- 内置定时任务（早盘 / 尾盘 / 收盘），可常驻后台自动运行。
- 深色主题 UI，结果表格支持排序、导出。

---

## 🛠 技术栈

- **界面**：PyQt6
- **网络**：aiohttp（全异步并发请求，Windows 下使用 `SelectorEventLoop` 以保证性能）
- **数据处理**：numpy、pypinyin（拼音搜索）、chinese_calendar（交易日历）
- **行情补充**：mootdx（通达信离线行情）、edge-tts（语音播报）、pymysql（可选数据库）

---

## 📦 安装与运行

### 方式一：直接运行源码

1. 安装 **Python 3.10+**（推荐 3.11）。
2. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

3. 启动（双击 `run.bat` 或手动运行）：

   ```bash
   python main.py
   ```

### 方式二：使用打包好的 exe

运行 `RaccoonQuant-Setup.exe` 自定义安装位置，或将 `dist/RaccoonQuant` 目录直接复制到任意目录双击 `RaccoonQuant.exe` 使用。

> MySQL 为**可选项**：未配置数据库时程序自动回退到本地文件缓存（`data/stock_list.json`），功能不受影响。

---

## 🔧 打包

```bash
build.bat
```

该脚本会自动探测 Python、安装 PyInstaller 与依赖、生成图标，并依次打包主程序、卸载程序与安装程序，产物位于 `dist/`：

- `dist/RaccoonQuant/RaccoonQuant.exe` —— 主程序
- `dist/uninstall.exe` —— 卸载程序
- `dist/RaccoonQuant-Setup.exe` —— 安装包

---

## 📁 目录结构

```
.
├── main.py                 # 主入口
├── config.py               # 全局配置（市值阈值、定时时间、主题色等）
├── data_fetcher.py         # 多数据源行情 / 列表 / 概念 / 资金流获取
├── data_fetcher_patch.py   # 运行时补丁（缓存 / 行情修正）
├── stock_filter.py         # 各策略选股与评分逻辑
├── stock_list_loader.py    # 股票列表加载（DB / 文件 / 平台）
├── stock_list_updater.py   # 股票列表更新
├── tdx_fetcher.py          # 通达信行情补充
├── mysql_store.py          # 可选 MySQL 持久化
├── scheduler.py            # 定时任务
├── ui/                     # PyQt6 界面（各 Tab + 组件）
├── sql/                    # 数据库脚本
├── build.bat               # 打包脚本
└── requirements.txt
```

---

## ⚠️ 免责声明

本项目所有选股结果均由公开行情数据经既定规则计算得到，仅用于技术研究与学习交流，
**不对任何投资决策负责**。请遵守所在地区法律法规，合理使用数据接口，控制请求频率。
