"""
MySQL 数据访问层 - 股票列表持久化

提供：连接配置持久化、建表、分页模糊查询、不存在则添加的批量插入。
配置保存于用户目录 ~/.raccoonquant/mysql_config.json，重启后自动恢复。
"""
import json
import os
import threading

import pymysql

DEFAULT_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'stock',
    'password': 'stock@123456',
    'database': 'stock',
}

CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.raccoonquant')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'mysql_config.json')

TABLE_NAME = 'thk_wx_stock_list'

_lock = threading.Lock()

# 表头字段与数据库字段映射（UI 层直接引用）
HEADER_FIELDS = ['code', 'name', 'name_initials', 'current_price',
                 'sector_concept', 'stock_exchange']


# ---------------------------------------------------------------- 配置 ----

def load_config():
    """读取持久化配置，缺失字段回退默认值"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in (saved or {}).items() if v not in (None, '')})
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """保存配置到本地（记住不丢失）"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 连接 ----

def _connect(cfg=None):
    cfg = cfg or load_config()
    return pymysql.connect(
        host=cfg.get('host', '127.0.0.1'),
        port=int(cfg.get('port', 3306)),
        user=cfg.get('user', 'stock'),
        password=cfg.get('password', 'stock@123456'),
        database=cfg.get('database', 'stock'),
        charset='utf8mb4',
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


def test_connection(cfg=None):
    """测试连接，成功返回 (True, '')，失败返回 (False, 错误信息)"""
    try:
        conn = _connect(cfg)
        conn.close()
        return True, ''
    except Exception as e:
        return False, str(e)


def ensure_table(cfg=None):
    """建表（不存在则创建），并保证 stock_exchange 列存在（兼容旧表 ALTER）"""
    sql = f"""
    CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
        `id` INT AUTO_INCREMENT PRIMARY KEY,
        `code` VARCHAR(10) NOT NULL,
        `name` VARCHAR(20) NOT NULL,
        `name_initials` VARCHAR(20) DEFAULT NULL,
        `current_price` DECIMAL(10,2) NOT NULL DEFAULT '0.00',
        `sector_concept` TEXT,
        `stock_exchange` VARCHAR(10) NOT NULL DEFAULT '',
        `update_time` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            # 兼容旧表：列不存在则补列（重复添加会报 1060 重复列，忽略）
            try:
                cur.execute(
                    f'ALTER TABLE `{TABLE_NAME}` ADD COLUMN '
                    f'`stock_exchange` VARCHAR(10) NOT NULL DEFAULT \'\' '
                    f'AFTER `sector_concept`')
            except Exception:
                pass
            try:
                cur.execute(f'ALTER TABLE `{TABLE_NAME}` ADD UNIQUE INDEX `uk_code` (`code`)')
            except Exception:
                pass  # 索引已存在或表结构不允许，忽略
            # 兼容旧数据：历史写入过 SSE/SZSE/BSE 全称，统一换算为 SH/SZ/BJ
            try:
                cur.execute(
                    f'UPDATE `{TABLE_NAME}` SET `stock_exchange` = CASE '
                    f"WHEN `stock_exchange` IN ('SSE') THEN 'SH' "
                    f"WHEN `stock_exchange` IN ('SZSE') THEN 'SZ' "
                    f"WHEN `stock_exchange` IN ('BSE') THEN 'BJ' "
                    f'ELSE `stock_exchange` END '
                    f"WHERE `stock_exchange` IN ('SSE','SZSE','BSE')")
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询 ----

def build_where(keyword):
    """根据关键词构造模糊查询 WHERE 子句（代码/名称/拼音首字母/板块概念）"""
    kw = str(keyword or '').strip()
    if not kw:
        return '', []
    like = f'%{kw}%'
    return ('WHERE `code` LIKE %s OR `name` LIKE %s OR '
            '`name_initials` LIKE %s OR `sector_concept` LIKE %s',
            [like, like, like, like])


def query_page(keyword='', page=1, page_size=10, cfg=None):
    """分页模糊查询，返回 (total, rows)。rows 为 dict 列表。"""
    where, params = build_where(keyword)
    offset = max(0, int(page) - 1) * int(page_size)
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) AS n FROM `{TABLE_NAME}` {where}', params)
            total = cur.fetchone()['n']
            cur.execute(
                f'SELECT `code`,`name`,`name_initials`,`current_price`,'
                f'`sector_concept`,`stock_exchange` '
                f'FROM `{TABLE_NAME}` {where} ORDER BY `id` LIMIT %s OFFSET %s',
                params + [int(page_size), offset])
            rows = cur.fetchall()
        return int(total), rows
    finally:
        conn.close()


def fetch_all_codes(cfg=None):
    """获取表内全部 code 集合（用于判断缺失股票）"""
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT `code` FROM `{TABLE_NAME}`')
            return {str(r['code']) for r in cur.fetchall()}
    finally:
        conn.close()


def fetch_all_stocks(cfg=None):
    """全量查询股票列表（供各栏目数据库优先加载）。

    返回行格式与 query_page 一致：
    {'code','name','name_initials','current_price','sector_concept','stock_exchange'}
    连接失败 / 表不存在 / 异常一律返回 []，由调用方回退本地文件或平台获取。
    """
    try:
        conn = _connect(cfg)
    except Exception:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT `code`,`name`,`name_initials`,`current_price`,'
                f'`sector_concept`,`stock_exchange` '
                f'FROM `{TABLE_NAME}` ORDER BY `id`')
            return list(cur.fetchall())
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_missing(stocks, cfg=None):
    """更新股票列表：表中不存在的股票则添加，已存在的更新最新字段。
    返回 (新增数量, 更新数量)。

    stocks: [{'code','name','name_initials','current_price','sector_concept',
              'stock_exchange'}, ...]
    不依赖唯一索引，先查已存在 code，再插缺失、更新已存在，稳妥且可重入。
    批量 executemany + 分块事务，避免 5000+ 条逐条 execute 的网络往返。
    """
    if not stocks:
        return 0, 0
    conn = _connect(cfg)
    added = 0
    updated = 0
    CHUNK = 500
    try:
        existing = fetch_all_codes(cfg)
        insert_rows = []
        update_rows = []
        for s in stocks:
            code = str(s.get('code') or '').strip()
            if not code:
                continue
            values = (
                str(s.get('name') or '')[:20],
                str(s.get('name_initials') or '')[:20],
                float(s.get('current_price') or 0),
                str(s.get('sector_concept') or ''),
                str(s.get('stock_exchange') or '')[:10],
            )
            if code in existing:
                update_rows.append(values + (code,))
            else:
                insert_rows.append((code,) + values)
                existing.add(code)
        with conn.cursor() as cur:
            for i in range(0, len(insert_rows), CHUNK):
                chunk = insert_rows[i:i + CHUNK]
                cur.executemany(
                    f'INSERT INTO `{TABLE_NAME}` '
                    f'(`code`,`name`,`name_initials`,`current_price`,'
                    f'`sector_concept`,`stock_exchange`) '
                    f'VALUES (%s,%s,%s,%s,%s,%s)', chunk)
            for i in range(0, len(update_rows), CHUNK):
                chunk = update_rows[i:i + CHUNK]
                cur.executemany(
                    f'UPDATE `{TABLE_NAME}` SET `name`=%s,`name_initials`=%s,'
                    f'`current_price`=%s,`sector_concept`=%s,`stock_exchange`=%s '
                    f'WHERE `code`=%s', chunk)
        conn.commit()
        return len(insert_rows), len(update_rows)
    finally:
        conn.close()
