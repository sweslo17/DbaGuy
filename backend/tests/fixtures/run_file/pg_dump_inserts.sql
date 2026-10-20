--
-- PostgreSQL database dump
--

\restrict 6jDw07Dbi2XyxZkS6fzEQsssvW5fdW9nuxrZjuA2PGdGvHTyc1vQ9WYbnn9IzxS

-- Dumped from database version 17.11 (Debian 17.11-1.pgdg13+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: zz_06_tool_tbl; Type: TABLE; Schema: public; Owner: dbaguy
--

CREATE TABLE public.zz_06_tool_tbl (
    id integer NOT NULL,
    label text NOT NULL,
    data bytea,
    amount numeric(30,10)
);


ALTER TABLE public.zz_06_tool_tbl OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE; Schema: public; Owner: dbaguy
--

CREATE SEQUENCE public.zz_06_tool_tbl_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: dbaguy
--

ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNED BY public.zz_06_tool_tbl.id;


--
-- Name: zz_06_tool_tbl id; Type: DEFAULT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl ALTER COLUMN id SET DEFAULT nextval('public.zz_06_tool_tbl_id_seq'::regclass);


--
-- Data for Name: zz_06_tool_tbl; Type: TABLE DATA; Schema: public; Owner: dbaguy
--

INSERT INTO public.zz_06_tool_tbl VALUES (1, 'it''s', '\x00ff', 12345678901234567890.1234567890);
INSERT INTO public.zz_06_tool_tbl VALUES (2, 'two
lines', NULL, NULL);
INSERT INTO public.zz_06_tool_tbl VALUES (3, '中文😀', '\xdeadbeef', -1.5000000000);


--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE SET; Schema: public; Owner: dbaguy
--

SELECT pg_catalog.setval('public.zz_06_tool_tbl_id_seq', 3, true);


--
-- Name: zz_06_tool_tbl zz_06_tool_tbl_pkey; Type: CONSTRAINT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl
    ADD CONSTRAINT zz_06_tool_tbl_pkey PRIMARY KEY (id);


--
-- Name: zz_06_tool_tbl_label; Type: INDEX; Schema: public; Owner: dbaguy
--

CREATE INDEX zz_06_tool_tbl_label ON public.zz_06_tool_tbl USING btree (label);


--
-- PostgreSQL database dump complete
--

\unrestrict 6jDw07Dbi2XyxZkS6fzEQsssvW5fdW9nuxrZjuA2PGdGvHTyc1vQ9WYbnn9IzxS

