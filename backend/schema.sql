-- MySQL 建表脚本（纯 SQL，供 storage.py 按分号切分执行）
-- 表设计：daily_snapshot 一列一指标；梯队/连板/热度/日志为独立子表

CREATE DATABASE IF NOT EXISTS ashare_review
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ashare_review;

-- 日汇总快照（每个交易日一行）
CREATE TABLE IF NOT EXISTS daily_snapshot (
  trade_date     DATE         NOT NULL COMMENT '交易日',
  week           VARCHAR(4)   NOT NULL COMMENT '星期, 如 周一',
  sh             DECIMAL(12,2) COMMENT '上证指数收盘',
  sh_p           DECIMAL(6,2)  COMMENT '上证涨跌幅%',
  sz             DECIMAL(12,2) COMMENT '深证成指',
  sz_p           DECIMAL(6,2),
  cy             DECIMAL(12,2) COMMENT '创业板指',
  cy_p           DECIMAL(6,2),
  hs             DECIMAL(12,2) COMMENT '沪深300',
  hs_p           DECIMAL(6,2),
  z5             DECIMAL(12,2) COMMENT '中证500',
  z5_p           DECIMAL(6,2),
  z1             DECIMAL(12,2) COMMENT '中证1000',
  z1_p           DECIMAL(6,2),
  kc             DECIMAL(12,2) COMMENT '科创50',
  kc_p           DECIMAL(6,2),
  amt            DECIMAL(8,2)  COMMENT '两市成交额(万亿)',
  amt_pct        DECIMAL(6,2)  COMMENT '成交额环比%',
  amt_est        DECIMAL(8,2)  COMMENT '盘中全天成交额预估(万亿,按昨日同时段占比外推)',
  main_net       DECIMAL(10,2) COMMENT '主力净流入(亿元,正为流入)',
  main_pct       DECIMAL(6,2)  COMMENT '主力净流入占成交额比例%',
  super_net      DECIMAL(10,2) COMMENT '超大单净流入(亿元)',
  big_net        DECIMAL(10,2) COMMENT '大单净流入(亿元)',
  up_cnt         INT           COMMENT '上涨家数',
  flat_cnt       INT           COMMENT '平盘家数',
  down_cnt       INT           COMMENT '下跌家数',
  zt_cnt         INT           COMMENT '涨停家数',
  dt_cnt         INT           COMMENT '跌停家数',
  zb_cnt         INT           COMMENT '炸板家数',
  zb_rate        DECIMAL(6,2)  COMMENT '炸板率%',
  lb_max         INT           COMMENT '最高连板',
  lianban_cnt    INT           COMMENT '连板家数(剔除首板)',
  mood           INT           COMMENT '市场热度/情绪分 0-100',
  yp_avg         DECIMAL(8,2)  COMMENT '昨涨停今表现-平均涨幅%',
  yp_red         INT           COMMENT '昨涨停今表现-红盘家数',
  yp_promo       INT           COMMENT '昨涨停今表现-晋级家数',
  data_status    TINYINT       NOT NULL DEFAULT 1 COMMENT '1=盘中轻量 2=收盘全量',
  data_time      VARCHAR(8)    COMMENT '盘中数据采集时间(HH:MM)，收盘全量为空',
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (trade_date)
) ENGINE=InnoDB COMMENT='A股每日复盘快照';

-- 涨停梯队（4进5/3进4/2进3/1进2/首板）
CREATE TABLE IF NOT EXISTS promo_tier (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date  DATE        NOT NULL,
  label       VARCHAR(10) NOT NULL COMMENT '梯队标签, 如 1进2 / 首板',
  win_cnt     INT NOT NULL DEFAULT 0,
  total_cnt   INT NOT NULL DEFAULT 0,
  sort_no     INT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_date_label (trade_date, label)
) ENGINE=InnoDB COMMENT='涨停晋级梯队汇总';

-- 梯队内个股明细
CREATE TABLE IF NOT EXISTS promo_stock (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date  DATE        NOT NULL,
  label       VARCHAR(10) NOT NULL,
  code        VARCHAR(10) NOT NULL,
  name        VARCHAR(30) NOT NULL,
  status      TINYINT     NOT NULL COMMENT '1=成功 2=炸板 0=失败',
  industry    VARCHAR(30),
  KEY idx_promo_date_label (trade_date, label)
) ENGINE=InnoDB COMMENT='涨停梯队个股明细';

-- 连板天梯（剔除首板）
CREATE TABLE IF NOT EXISTS lianban_stock (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date  DATE        NOT NULL,
  code        VARCHAR(10) NOT NULL,
  name        VARCHAR(30) NOT NULL,
  boards      INT NOT NULL COMMENT '连板数',
  industry    VARCHAR(30),
  KEY idx_lb_date (trade_date)
) ENGINE=InnoDB COMMENT='连板天梯';

-- 概念热度（涨停家数排行）
CREATE TABLE IF NOT EXISTS concept_heat (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date  DATE        NOT NULL,
  concept     VARCHAR(30) NOT NULL,
  zt_cnt      INT NOT NULL,
  rank_no     INT NOT NULL,
  members     TEXT             COMMENT '该题材当日涨停个股名单(JSON数组)',
  UNIQUE KEY uk_date_concept (trade_date, concept)
) ENGINE=InnoDB COMMENT='概念热度排行';

CREATE INDEX idx_snapshot_date ON daily_snapshot(trade_date);

-- 采集任务日志（管理后台用）
CREATE TABLE IF NOT EXISTS collect_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_date  DATE         NOT NULL COMMENT '采集目标交易日',
  task_type   VARCHAR(16)  NOT NULL COMMENT 'full=收盘全量 intraday=盘中轮询 manual=手动补采',
  status      VARCHAR(12)  NOT NULL COMMENT 'running/success/failed',
  started_at  DATETIME     NOT NULL,
  finished_at DATETIME,
  duration_s  INT,
  rows_written INT DEFAULT 0 COMMENT '写入记录数',
  error_msg   TEXT,
  trigger_by  VARCHAR(32) DEFAULT 'scheduler' COMMENT 'scheduler/manual/admin:<user>'
) ENGINE=InnoDB COMMENT='采集任务日志';

CREATE INDEX idx_collog_date ON collect_log(trade_date);
CREATE INDEX idx_collog_time ON collect_log(started_at);

-- 数据源健康探测记录
CREATE TABLE IF NOT EXISTS source_health (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_name VARCHAR(40)  NOT NULL COMMENT '接口名',
  checked_at  DATETIME     NOT NULL,
  status      VARCHAR(10)  NOT NULL COMMENT 'ok/fail',
  latency_ms  INT,
  error_msg   VARCHAR(255)
) ENGINE=InnoDB COMMENT='数据源健康探测';

CREATE INDEX idx_health_time ON source_health(checked_at);
