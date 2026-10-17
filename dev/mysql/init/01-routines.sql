-- Stored programs, written the way the mysql client needs them (and the way DbaGuy splits them): DELIMITER lines.
USE dbaguy;

DELIMITER $$

CREATE DEFINER = `dbaguy`@`%` PROCEDURE two_results(IN n INT)
BEGIN
    SELECT id, label FROM big_table WHERE id <= n ORDER BY id;
    SELECT COUNT(*) AS total FROM big_table;
END$$

CREATE DEFINER = `dbaguy`@`%` PROCEDURE fail_after_select()
BEGIN
    SELECT 1 AS first;
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'boom after the first result';
END$$

CREATE DEFINER = `dbaguy`@`%` FUNCTION add_numbers(a INT, b INT) RETURNS INT DETERMINISTIC
BEGIN
    RETURN a + b;
END$$

CREATE DEFINER = `dbaguy`@`%` TRIGGER orders_note BEFORE INSERT ON orders FOR EACH ROW
BEGIN
    IF NEW.note IS NULL THEN
        SET NEW.note = '';
    END IF;
END$$

CREATE DEFINER = `dbaguy`@`%` EVENT purge_job_log
ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00'
DO BEGIN
    DELETE FROM job_log WHERE created_at < NOW() - INTERVAL 30 DAY;
END$$

DELIMITER ;
