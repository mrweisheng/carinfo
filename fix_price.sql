-- 价格数据修复SQL - 一次性解决所有问题

-- 修复只有现价的记录（如 HKD$100,000）
UPDATE vehicles 
SET 
    current_price = CAST(REPLACE(REPLACE(REPLACE(price, 'HKD$', ''), 'HKD', ''), ',', '') AS DECIMAL(10,2)),
    original_price = CAST(REPLACE(REPLACE(REPLACE(price, 'HKD$', ''), 'HKD', ''), ',', '') AS DECIMAL(10,2))
WHERE 
    price REGEXP '^HKD\\$?[0-9,]+$'
    AND price LIKE '%,%';

-- 修复有现价和原价的记录（如 HKD$62,000[原價$68,000]）
UPDATE vehicles 
SET 
    current_price = CAST(REPLACE(REPLACE(SUBSTRING_INDEX(REPLACE(price, 'HKD$', ''), '[', 1), ',', ''), '$', '') AS DECIMAL(10,2)),
    original_price = CAST(REPLACE(REPLACE(SUBSTRING_INDEX(SUBSTRING_INDEX(price, '原價', -1), ']', 1), ',', ''), '$', '') AS DECIMAL(10,2))
WHERE 
    price LIKE '%[%原價%]%'
    AND price LIKE '%,%';

-- 验证修复结果
SELECT 
    COUNT(*) as total_records,
    COUNT(CASE WHEN current_price > 1000 THEN 1 END) as fixed_records,
    AVG(current_price) as avg_price
FROM vehicles 
WHERE price IS NOT NULL;
