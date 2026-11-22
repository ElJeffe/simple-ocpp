"""Persistent state: chargers, RFID tags and groups, charge sessions, energy
and settings, in one SQLite database.

Use it as `from chargepoint import store` and `store.add_tag(...)`: everything
public in the modules below is available here.
"""

from .auth import (
    LOGIN_DAYS,
    auth_enabled,
    check_login_token,
    check_password,
    clear_password,
    make_login_token,
    set_password,
)
from .chargers import (
    ChargerJSON,
    ChargerState,
    ConnectorInfo,
    LockConfig,
    delete_charger,
    get_state,
    load_registry,
    registry,
    save_registry,
)
from .database import backup, backup_bytes, db, ensure_data_dir, init_db, now_iso
from .energy import (
    EnergyPeriod,
    EnergyUsage,
    InitialEnergy,
    clear_initial_energy,
    energy_usage,
    fiscal_year,
    initial_energy,
    record_energy,
    set_initial_energy,
)
from .sessions import (
    NO_STOP_RECEIVED,
    SessionRecord,
    SessionSummary,
    open_sessions,
    session_summary,
    close_open_sessions,
    count_sessions,
    list_sessions,
    session_connector,
    start_session,
    stop_session,
)
from .settings import (
    DecimalSeparator,
    Localisation,
    NetworkSettings,
    TimeFormat,
    currency,
    localisation,
    set_currency,
    set_localisation,
    active_network,
    fiscal_year_start,
    get_setting,
    network_overrides,
    network_settings,
    saved_network_settings,
    set_active_network,
    set_fiscal_year_start,
    set_network_settings,
    set_setting,
)
from .tariffs import (
    Tariff,
    add_tariff,
    local_day,
    price_url,
    prices,
    remove_tariff,
    set_price_url,
    set_tariffs,
    tariffs,
    update_tariff,
)
from .tags import (
    GroupRecord,
    TagRecord,
    add_group,
    add_tag,
    groups,
    load_tags,
    remove_group,
    remove_tag,
    sorted_groups,
    sorted_tags,
    tag_key,
    tag_parent,
    tag_status,
    tags,
    update_tag,
)

__all__ = [
    "DecimalSeparator", "Localisation", "TimeFormat", "localisation", "set_localisation",
    "backup", "backup_bytes",
    "LOGIN_DAYS", "auth_enabled", "check_login_token", "check_password", "clear_password",
    "make_login_token", "set_password",
    "SessionSummary", "session_summary", "open_sessions", "Tariff", "currency", "local_day", "prices",
    "set_tariffs", "tariffs", "add_tariff", "update_tariff", "remove_tariff", "set_currency", "price_url", "set_price_url",
    "NO_STOP_RECEIVED", "ChargerJSON", "ChargerState", "ConnectorInfo", "EnergyPeriod",
    "EnergyUsage", "GroupRecord", "InitialEnergy", "LockConfig", "NetworkSettings",
    "SessionRecord", "TagRecord", "active_network", "add_group", "add_tag",
    "clear_initial_energy", "close_open_sessions", "count_sessions", "db", "delete_charger",
    "energy_usage", "ensure_data_dir", "fiscal_year", "fiscal_year_start", "get_setting",
    "get_state", "groups", "init_db", "initial_energy", "list_sessions", "load_registry",
    "load_tags", "network_overrides", "network_settings", "now_iso", "record_energy",
    "registry", "remove_group", "remove_tag", "save_registry", "saved_network_settings",
    "session_connector", "set_active_network", "set_fiscal_year_start", "set_initial_energy",
    "set_network_settings", "set_setting", "sorted_groups", "sorted_tags", "start_session",
    "stop_session", "tag_key", "tag_parent", "tag_status", "tags", "update_tag",
]
