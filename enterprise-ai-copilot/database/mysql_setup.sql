-- Run ONCE as a MySQL administrator (e.g. root):
--   mysql -u root -p < database/mysql_setup.sql
-- Then change the password below BEFORE running, and put the same password in .env (MYSQL_PASSWORD).

CREATE DATABASE IF NOT EXISTS enterprise_copilot
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- Replace CHANGE_ME_STRONG_PASSWORD with your own strong password.
CREATE USER IF NOT EXISTS 'copilot_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON enterprise_copilot.* TO 'copilot_user'@'localhost';
FLUSH PRIVILEGES;

-- Tables are created by the application:   flask --app app init-db
-- (or inspect the DDL first:                flask --app app export-schema > database/schema.sql)
SELECT 'Database enterprise_copilot is ready' AS status;
