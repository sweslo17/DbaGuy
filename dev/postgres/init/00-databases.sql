CREATE DATABASE analytics;
\connect analytics
CREATE TABLE public.page_views (
    id bigserial PRIMARY KEY,
    path text NOT NULL,
    viewed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.page_views (path) SELECT '/page/' || g FROM generate_series(1, 50) g;
\connect dbaguy
