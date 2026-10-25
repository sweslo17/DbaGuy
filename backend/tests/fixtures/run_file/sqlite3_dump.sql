PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE zz_06_tool_tbl (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, data BLOB, v);
INSERT INTO zz_06_tool_tbl VALUES(1,'it''s',X'00ff','007');
INSERT INTO zz_06_tool_tbl VALUES(2,unistr('two\u000alines'),NULL,1.5);
INSERT INTO zz_06_tool_tbl VALUES(3,'中文😀',NULL,0);
INSERT INTO sqlite_sequence VALUES('zz_06_tool_tbl',3);
CREATE TRIGGER zz_06_tool_tbl_ai AFTER INSERT ON zz_06_tool_tbl BEGIN UPDATE zz_06_tool_tbl SET v = coalesce(v, 0) WHERE id = new.id; END;
COMMIT;
