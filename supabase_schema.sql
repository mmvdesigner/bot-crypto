-- ============================================
-- Bot Crypto — Schema Supabase (executado)
-- ============================================

-- trades_log
CREATE TABLE IF NOT EXISTS trades_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    entry_price NUMERIC NOT NULL,
    exit_price NUMERIC,
    amount NUMERIC NOT NULL,
    pnl NUMERIC,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    strategy_name TEXT NOT NULL DEFAULT 'volume_expansion'
);

CREATE INDEX IF NOT EXISTS idx_trades_log_timestamp ON trades_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_log_status   ON trades_log(status);

-- bot_state (singleton)
CREATE TABLE IF NOT EXISTS bot_state (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'STOPPED' CHECK (status IN ('RUNNING', 'STOPPED', 'ERROR')),
    current_position TEXT NOT NULL DEFAULT 'FLAT' CHECK (current_position IN ('FLAT', 'LONG', 'SHORT')),
    last_squeeze_high NUMERIC,
    last_squeeze_low NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Trigger updated_at
CREATE OR REPLACE FUNCTION update_bot_state_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bot_state_updated ON bot_state;
CREATE TRIGGER trg_bot_state_updated
    BEFORE UPDATE ON bot_state
    FOR EACH ROW
    EXECUTE FUNCTION update_bot_state_timestamp();

-- Singleton
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_state_singleton ON bot_state((TRUE));

-- RLS
ALTER TABLE trades_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_state   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "auth_read_trades_log"  ON trades_log FOR SELECT USING (auth.role() = 'authenticated');
CREATE POLICY "auth_read_bot_state"   ON bot_state   FOR SELECT USING (auth.role() = 'authenticated');
CREATE POLICY "auth_update_bot_state" ON bot_state   FOR UPDATE USING (auth.role() = 'authenticated');
CREATE POLICY "svc_all_trades_log"    ON trades_log FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "svc_all_bot_state"     ON bot_state   FOR ALL USING (auth.role() = 'service_role');

-- Linha inicial
INSERT INTO bot_state (status, current_position)
VALUES ('STOPPED', 'FLAT')
ON CONFLICT ((TRUE)) DO NOTHING;
