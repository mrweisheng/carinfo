/*
 Navicat Premium Dump SQL

 Source Server         : car
 Source Server Type    : MySQL
 Source Server Version : 80042 (8.0.42-0ubuntu0.20.04.1)
 Source Host           : 103.117.122.192:3306
 Source Schema         : car_info_db

 Target Server Type    : MySQL
 Target Server Version : 80042 (8.0.42-0ubuntu0.20.04.1)
 File Encoding         : 65001

 Date: 18/08/2025 09:57:04
*/

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for user_browsing_history
-- ----------------------------
DROP TABLE IF EXISTS `user_browsing_history`;
CREATE TABLE `user_browsing_history`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '浏览记录ID（自增主键）',
  `user_id` bigint NOT NULL COMMENT '用户ID',
  `vehicle_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '车辆ID',
  `browse_time` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '浏览时间',
  `ip_address` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT 'IP地址',
  `user_agent` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '用户代理',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_vehicle_id`(`vehicle_id` ASC) USING BTREE,
  INDEX `idx_browse_time`(`browse_time` ASC) USING BTREE,
  CONSTRAINT `user_browsing_history_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_browsing_history_ibfk_2` FOREIGN KEY (`vehicle_id`) REFERENCES `vehicles` (`vehicle_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户浏览历史表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_favorites
-- ----------------------------
DROP TABLE IF EXISTS `user_favorites`;
CREATE TABLE `user_favorites`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '收藏ID（自增主键）',
  `user_id` bigint NOT NULL COMMENT '用户ID',
  `vehicle_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '车辆ID',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '收藏时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_user_vehicle`(`user_id` ASC, `vehicle_id` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_vehicle_id`(`vehicle_id` ASC) USING BTREE,
  INDEX `idx_created_at`(`created_at` ASC) USING BTREE,
  CONSTRAINT `user_favorites_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_favorites_ibfk_2` FOREIGN KEY (`vehicle_id`) REFERENCES `vehicles` (`vehicle_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户收藏表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_messages
-- ----------------------------
DROP TABLE IF EXISTS `user_messages`;
CREATE TABLE `user_messages`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '消息ID（自增主键）',
  `user_id` bigint NOT NULL COMMENT '用户ID',
  `title` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '消息标题',
  `content` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '消息内容',
  `type` tinyint NOT NULL DEFAULT 1 COMMENT '消息类型：1=系统消息, 2=车辆相关, 3=用户相关',
  `is_read` tinyint NOT NULL DEFAULT 0 COMMENT '是否已读：1=已读, 0=未读',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `read_at` timestamp NULL DEFAULT NULL COMMENT '阅读时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_type`(`type` ASC) USING BTREE,
  INDEX `idx_is_read`(`is_read` ASC) USING BTREE,
  INDEX `idx_created_at`(`created_at` ASC) USING BTREE,
  CONSTRAINT `user_messages_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户消息表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_sessions
-- ----------------------------
DROP TABLE IF EXISTS `user_sessions`;
CREATE TABLE `user_sessions`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '会话ID（自增主键）',
  `user_id` bigint NOT NULL COMMENT '用户ID',
  `token` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'JWT令牌',
  `refresh_token` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '刷新令牌',
  `ip_address` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT 'IP地址',
  `user_agent` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '用户代理',
  `expires_at` timestamp NOT NULL COMMENT '过期时间',
  `is_active` tinyint NOT NULL DEFAULT 1 COMMENT '是否活跃：1=是, 0=否',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_token`(`token` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_expires_at`(`expires_at` ASC) USING BTREE,
  INDEX `idx_is_active`(`is_active` ASC) USING BTREE,
  CONSTRAINT `user_sessions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户会话表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for users
-- ----------------------------
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '用户ID（自增主键）',
  `username` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用户名（用作登录）',
  `email` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '邮箱（客户信息，可选）',
  `password` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '密码',
  `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '联络电话',
  `avatar` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL,
  `real_name` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '联络人姓名',
  `status` tinyint NULL DEFAULT 1 COMMENT '用户状态：1=正常, 2=禁用, 3=待验证',
  `role` tinyint NULL DEFAULT 1 COMMENT '用户角色：1=普通用户, 2=管理员, 3=超级管理员',
  `last_login_at` datetime NULL DEFAULT NULL,
  `last_login_ip` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `gender` enum('male','female','other') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '性别',
  `region` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '地区（根据IP自动获取）',
  `ip_address` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '注册时的IP地址',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_username`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_2`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_3`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_4`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_5`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_6`(`username` ASC) USING BTREE,
  UNIQUE INDEX `username_7`(`username` ASC) USING BTREE,
  UNIQUE INDEX `uk_email`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_2`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_3`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_4`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_5`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_6`(`email` ASC) USING BTREE,
  UNIQUE INDEX `email_7`(`email` ASC) USING BTREE,
  INDEX `idx_phone`(`phone` ASC) USING BTREE,
  INDEX `idx_status`(`status` ASC) USING BTREE,
  INDEX `idx_role`(`role` ASC) USING BTREE,
  INDEX `idx_created_at`(`created_at` ASC) USING BTREE,
  INDEX `idx_gender`(`gender` ASC) USING BTREE,
  INDEX `idx_region`(`region` ASC) USING BTREE,
  INDEX `idx_ip_address`(`ip_address` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 5 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for vehicle_images
-- ----------------------------
DROP TABLE IF EXISTS `vehicle_images`;
CREATE TABLE `vehicle_images`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `vehicle_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '车辆编号（关联vehicles表）',
  `image_url` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '图片URL',
  `local_path` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '本地图片路径',
  `image_order` int NULL DEFAULT 0 COMMENT '图片顺序（0,1,2,3...）',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_vehicle_id`(`vehicle_id` ASC) USING BTREE,
  INDEX `idx_image_order`(`image_order` ASC) USING BTREE,
  CONSTRAINT `vehicle_images_ibfk_1` FOREIGN KEY (`vehicle_id`) REFERENCES `vehicles` (`vehicle_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 95358 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '车辆图片表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for vehicles
-- ----------------------------
DROP TABLE IF EXISTS `vehicles`;
CREATE TABLE `vehicles`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `vehicle_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '车辆编号（网站原始编号）',
  `vehicle_type` tinyint NOT NULL DEFAULT 1 COMMENT '车辆类型：1=私家车, 2=客货车, 3=货车, 4=电单车, 5=经典车',
  `vehicle_status` tinyint NOT NULL DEFAULT 1 COMMENT '车辆状态：1=未售, 2=已售',
  `page_number` int NOT NULL COMMENT '来源页面编号',
  `car_number` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '车辆编号',
  `car_url` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '车辆详情页URL',
  `car_category` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '车类',
  `car_brand` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '车厂',
  `car_model` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '型号',
  `fuel_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '燃料类型',
  `seats` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '座位数',
  `engine_volume` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '发动机容积',
  `transmission` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '传动方式',
  `year` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '年份',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '简评/描述',
  `price` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '售价',
  `current_price` decimal(10, 2) NULL DEFAULT NULL COMMENT '现价（数字格式）',
  `original_price` decimal(10, 2) NULL DEFAULT NULL COMMENT '原价（数字格式）',
  `contact_info` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '联络人资料',
  `update_date` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '更新日期',
  `extra_fields` json NULL COMMENT '扩展字段（JSON格式存储类型特有属性）',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `contact_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '联系人姓名',
  `phone_number` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '联系电话',
  `is_special_offer` tinyint NOT NULL DEFAULT 0 COMMENT '是否特价车辆：1=是, 0=否',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_vehicle_id`(`vehicle_id` ASC) USING BTREE,
  INDEX `idx_vehicle_id`(`vehicle_id` ASC) USING BTREE,
  INDEX `idx_vehicle_type_status`(`vehicle_type` ASC, `vehicle_status` ASC) USING BTREE,
  INDEX `idx_car_brand`(`car_brand` ASC) USING BTREE,
  INDEX `idx_year`(`year` ASC) USING BTREE,
  INDEX `idx_created_at`(`created_at` ASC) USING BTREE,
  INDEX `idx_current_price`(`current_price` ASC) USING BTREE,
  INDEX `idx_original_price`(`original_price` ASC) USING BTREE,
  INDEX `idx_contact_name`(`contact_name` ASC) USING BTREE,
  INDEX `idx_phone_number`(`phone_number` ASC) USING BTREE,
  INDEX `idx_is_special_offer`(`is_special_offer` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 20748 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '车辆基础信息表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- View structure for vehicle_summary
-- ----------------------------
DROP VIEW IF EXISTS `vehicle_summary`;
CREATE ALGORITHM = UNDEFINED SQL SECURITY DEFINER VIEW `vehicle_summary` AS select `v`.`id` AS `id`,`v`.`vehicle_id` AS `vehicle_id`,`v`.`vehicle_type` AS `vehicle_type`,`v`.`vehicle_status` AS `vehicle_status`,`v`.`page_number` AS `page_number`,`v`.`car_number` AS `car_number`,`v`.`car_url` AS `car_url`,`v`.`car_category` AS `car_category`,`v`.`car_brand` AS `car_brand`,`v`.`car_model` AS `car_model`,`v`.`fuel_type` AS `fuel_type`,`v`.`seats` AS `seats`,`v`.`engine_volume` AS `engine_volume`,`v`.`transmission` AS `transmission`,`v`.`year` AS `year`,`v`.`description` AS `description`,`v`.`price` AS `price`,`v`.`contact_info` AS `contact_info`,`v`.`update_date` AS `update_date`,`v`.`created_at` AS `created_at`,`v`.`updated_at` AS `updated_at`,count(`vi`.`id`) AS `image_count`,group_concat(`vi`.`image_url` order by `vi`.`image_order` ASC separator '\n') AS `all_image_urls` from (`vehicles` `v` left join `vehicle_images` `vi` on((`v`.`vehicle_id` = `vi`.`vehicle_id`))) group by `v`.`id`;

SET FOREIGN_KEY_CHECKS = 1;
