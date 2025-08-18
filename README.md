# 28car.com 汽车信息爬虫

## 项目简介

这是一个专门爬取28car.com香港二手车网站的高级爬虫程序，采用多种反爬虫策略确保稳定运行，并集成了 MySQL 数据库进行数据持久化存储。

## 数据库配置

### MySQL 数据库设置

项目使用 MySQL 数据库进行数据存储，支持数据去重和进度跟踪。

#### 数据库连接信息
```python
DB_CONFIG = {
    'host': '127.0.0.1',
    'user': 'root',
    'password': '1qaz!QAZ2wsx@WSX',
    'database': 'carinfo数据库',
    'charset': 'utf8mb4',
    'cursorclass': DictCursor
}
```

#### 数据库表结构

**car_info 表** - 存储车辆信息
```sql
CREATE TABLE car_info (
    id INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_id VARCHAR(255) UNIQUE NOT NULL,
    vehicle_type INT COMMENT '车辆类型：1=私家车, 2=客货车, 3=货车, 4=电单车, 5=经典车',
    vehicle_status INT COMMENT '车辆状态：1=未售, 2=已售',
    page_number INT COMMENT '爬取的页面编号',
    car_number VARCHAR(255) COMMENT '车辆编号',
    car_url TEXT COMMENT '车辆详情页URL',
    car_category VARCHAR(255) COMMENT '车辆类别',
    car_brand VARCHAR(255) COMMENT '车厂品牌',
    car_model VARCHAR(255) COMMENT '车型型号',
    fuel_type VARCHAR(255) COMMENT '燃料类型',
    seats VARCHAR(255) COMMENT '座位数',
    engine_volume VARCHAR(255) COMMENT '发动机容积',
    transmission VARCHAR(255) COMMENT '传动方式',
    year VARCHAR(255) COMMENT '年份',
    description TEXT COMMENT '车辆描述',
    price VARCHAR(255) COMMENT '售价',
    contact_info TEXT COMMENT '联络人资料',
    update_date VARCHAR(255) COMMENT '更新日期',
    image_urls TEXT COMMENT '图片URL列表',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**page_processing 表** - 记录页面处理状态
```sql
CREATE TABLE page_processing (
    id INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_type INT COMMENT '车辆类型',
    vehicle_status INT COMMENT '车辆状态',
    page_number INT COMMENT '页面编号',
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '处理时间',
    UNIQUE KEY unique_page (vehicle_type, vehicle_status, page_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 数据库初始化

1. **运行建表脚本**
   ```bash
   mysql -u root -p < mysql_tables.sql
   ```

2. **测试数据库连接**
   ```bash
   python test_mysql_connection.py
   ```

## 反爬虫优化策略

### 1. **智能请求头管理**
- 随机User-Agent轮换
- 动态Accept-Language设置
- 完整的浏览器请求头模拟

### 2. **自适应延迟策略**
- 根据请求频率动态调整延迟时间
- 随机抖动避免规律性
- 指数退避重试机制

### 3. **会话管理**
- 定期轮换会话避免被识别
- 动态Cookie生成
- 请求频率监控

### 4. **反爬检测与处理**
- 自动检测反爬虫页面
- 智能等待和重试
- 错误恢复机制

### 5. **请求重试机制**
- 最多3次重试
- 指数退避算法
- 详细错误日志

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

```bash
python carinfo.py
```

选择爬取模式：
1. 私家车-未售
2. 客货车-未售
3. 货车-未售
4. 电单车-未售
5. 经典车-未售
6. 爬取整个平台所有数据
7. 查看数据库统计信息

## 车辆分类

支持5种车辆类型：
- 私家车 (Private Car)
- 客货车 (Van / Light Goods Vehicle)
- 货车 (Truck / Lorry)
- 电单车 (Motorcycle)
- 经典车 (Classic Car)

## 数据库功能

### 数据去重
- 基于 `vehicle_id` 的唯一约束
- 自动跳过已爬取的车辆
- 支持数据更新和覆盖

### 进度跟踪
- 记录已处理的页面
- 支持中断后继续爬取
- 避免重复处理同一页面

### 统计功能
- 实时显示爬取进度
- 按类型统计车辆数量
- 显示已处理页面数量

## 输出文件

- `car_data/` - 数据输出目录
- `car_data_*.xlsx` - 各类型车辆数据
- `all_car_data.xlsx` - 汇总数据文件

## 反爬虫特性

### 请求头优化
```python
# 随机User-Agent
user_agents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36...',
    # ...更多UA
]
```

### 延迟策略
```python
# 自适应延迟
if request_count > 50:
    delay = random.uniform(3, 6)  # 高频率
elif request_count > 20:
    delay = random.uniform(2, 4)  # 中等频率
else:
    delay = random.uniform(1, 3)  # 低频率
```

### 会话轮换
```python
# 每100个请求轮换一次会话
if request_count % 100 == 0:
    rotate_session()
```

## 注意事项

1. **数据库配置** - 确保 MySQL 服务正常运行，数据库连接信息正确
2. **遵守robots.txt** - 请确保遵守网站的爬虫协议
3. **合理使用** - 避免对目标网站造成过大压力
4. **代理配置** - 如需使用代理，请取消注释相关代码
5. **频率控制** - 可根据需要调整延迟参数

## 技术栈

- Python 3.8+
- Requests (HTTP请求)
- BeautifulSoup4 (HTML解析)
- Pandas (数据处理)
- OpenPyXL (Excel输出)
- Fake-UserAgent (User-Agent生成)
- PyMySQL (MySQL数据库连接)

## 许可证

本项目仅供学习和研究使用，请勿用于商业用途。 