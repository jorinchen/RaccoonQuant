"""
安装目录 / 数据目录管理

应用以"安装到任意分区目录"的模式运行：
- 打包模式（PyInstaller）：安装目录 = RaccoonQuant.exe 所在目录
  （由安装程序让用户自定义选择，如 D:\RaccoonQuant）
- 源码模式：安装目录 = 项目根目录

股票列表等数据统一保存在 <安装目录>\data\ 下，
各栏目加载列表时若数据库不可用/为空，则从该本地文件读取，无需重复联网。
"""
import json
import os
import sys


def get_install_dir():
    """获取应用安装目录（exe 所在目录或项目根目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_data_dir():
    """数据目录：<安装目录>\data"""
    return os.path.join(get_install_dir(), 'data')


def get_log_dir():
    """日志目录：<安装目录>\log"""
    return os.path.join(get_install_dir(), 'log')


def ensure_log_dir():
    """确保日志目录存在，返回其路径"""
    d = get_log_dir()
    os.makedirs(d, exist_ok=True)
    return d


def get_stock_list_file():
    """股票列表本地缓存文件路径：<安装目录>\data\stock_list.json"""
    return os.path.join(get_data_dir(), 'stock_list.json')


def ensure_data_dir():
    """确保数据目录存在，返回其路径"""
    d = get_data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def normalize_stock(item):
    """将任意来源的股票行统一为各栏目消费所需的完整字段格式。

    兼容三种来源：
    - 数据库行（code/name/name_initials/current_price/sector_concept/stock_exchange）
    - 平台列表（code/name/market_cap/pe/sector）
    - 本地缓存文件（上述混合）
    统一输出后同时填充 sector 与 sector_concept，保证新旧消费方都不缺字段。
    """
    if not isinstance(item, dict):
        return None
    code = str(item.get('code') or '').strip()
    name = str(item.get('name') or '').strip()
    if not code or not name:
        return None
    try:
        current_price = float(item.get('current_price') or item.get('current') or 0)
    except (TypeError, ValueError):
        current_price = 0.0
    try:
        market_cap = float(item.get('market_cap') or 0)
    except (TypeError, ValueError):
        market_cap = 0.0
    try:
        pe = float(item.get('pe') or 0)
    except (TypeError, ValueError):
        pe = 0.0
    sector = str(item.get('sector_concept') or item.get('sector') or '').strip()
    return {
        'code': code,
        'name': name,
        'name_initials': str(item.get('name_initials') or '').strip(),
        'current_price': current_price,
        'market_cap': market_cap,
        'pe': pe,
        'sector_concept': sector,
        'sector': sector,
        'stock_exchange': str(item.get('stock_exchange') or '').strip(),
    }


def save_stock_list(stocks):
    """保存股票列表到安装目录数据文件（临时文件 + 原子替换，防止写一半损坏）"""
    if not stocks:
        return False
    tmp = None
    try:
        ensure_data_dir()
        fp = get_stock_list_file()
        tmp = fp + '.tmp'
        rows = []
        for s in stocks:
            n = normalize_stock(s)
            if n:
                rows.append(n)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, fp)
        return True
    except Exception:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_stock_list_file():
    """从安装目录数据文件加载股票列表（不存在/损坏返回空列表）"""
    fp = get_stock_list_file()
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [n for n in (normalize_stock(s) for s in data) if n]
    except Exception:
        return []


def stock_list_file_age():
    """返回本地股票列表文件的年龄（秒），文件不存在返回 None"""
    fp = get_stock_list_file()
    if not os.path.exists(fp):
        return None
    try:
        return os.path.getmtime(fp)
    except Exception:
        return None
