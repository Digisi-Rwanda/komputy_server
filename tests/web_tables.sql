-- Minimal copies of the web app's tables the engine reads (columns the engine maps in komputy/web.py).
CREATE TABLE IF NOT EXISTS public.users (id varchar(26) PRIMARY KEY, email text NOT NULL, name text NOT NULL, is_active boolean NOT NULL DEFAULT true, platform_role varchar(16));
CREATE TABLE IF NOT EXISTS public.tenants (id varchar(26) PRIMARY KEY, name text NOT NULL, status varchar(32) NOT NULL DEFAULT 'active', currency varchar(8) NOT NULL DEFAULT 'USD',
  timezone varchar(64) NOT NULL DEFAULT 'Africa/Kigali', reporting_cadence varchar(16) NOT NULL DEFAULT 'monthly', fiscal_year_end varchar(5) NOT NULL DEFAULT '12-31',
  authz_version integer NOT NULL DEFAULT 1, engine_version_pin text, deleted_at timestamptz);
CREATE TABLE IF NOT EXISTS public.tenant_memberships (id varchar(26) PRIMARY KEY, user_id varchar(26) NOT NULL, tenant_id varchar(26) NOT NULL, role varchar(32) NOT NULL DEFAULT 'analyst',
  workspace_role varchar(16) NOT NULL DEFAULT 'member', status varchar(32) NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS public.member_service_roles (id varchar(26) PRIMARY KEY, membership_id varchar(26) NOT NULL, service varchar(16) NOT NULL, role varchar(16) NOT NULL);
CREATE TABLE IF NOT EXISTS public.user_notifications (id varchar(26) PRIMARY KEY, user_id varchar(26) NOT NULL, tenant_id varchar(26) NOT NULL, kind varchar(32) NOT NULL, title varchar(255) NOT NULL,
  body text NOT NULL, href text, is_read boolean NOT NULL DEFAULT false, read_at timestamptz, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.audit_logs (id varchar(26) PRIMARY KEY, tenant_id varchar(26), user_id varchar(26), event_type varchar(50) NOT NULL, status varchar(20) NOT NULL DEFAULT 'success',
  ip_address_hash varchar(64), user_agent_hash varchar(64), details json, error_code varchar(50), created_at timestamptz NOT NULL DEFAULT now());
