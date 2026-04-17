-- Create a separate schema for n8n so its tables don't pollute public
-- n8n uses this when DB_POSTGRESDB_SCHEMA=n8n is set
CREATE SCHEMA IF NOT EXISTS n8n;
GRANT ALL PRIVILEGES ON SCHEMA n8n TO agent;
