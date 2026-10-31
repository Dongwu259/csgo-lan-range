#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>
#include <dbi>

#define PLUGIN_VERSION "1.2.3"

/*
    lan_fix.sp  --  console/RCON-callable live skin application + bot probing.

    Why this exists: kgns' weapons/gloves commands are player-context (handlers
    start with IsValidClient(client)), so an external program driving the server
    over RCON arrives as client 0 and is refused. And that plugin caches each
    selection in g_iSkins[] at connect time, so editing its SQLite rows does
    nothing until the player reconnects.

    This writes the same net properties kgns writes (copied from
    scripting/weapons.sp SetWeaponProps and the wearable block of
    scripting/gloves.sp) onto the live entities, and persists the same columns
    into the same tables, so the selection survives a reconnect inside kgns'
    own menu.

    Only validated integers/floats are ever interpolated into SQL, and every
    column is checked against PRAGMA table_info first. Name tags are not
    accepted here on purpose -- use !nametag in game.

    sm_lanfix_botmem is a different beast: bot_stuff.smx overwrites four floats
    inside the engine's BotProfile every tick, so the numbers botprofile.db
    asks for are NOT necessarily the numbers in effect. The bot's profile
    struct is read back through the same CCSBot::m_pLocalProfile gamedata
    offset bot_stuff itself uses, which is the only way to see what a bot is
    actually running on. A raw little-endian dump of the struct follows so the
    tooling can locate the fields bot_stuff does not touch.
*/

ConVar g_ConnName;
StringMap g_WeaponCols;
StringMap g_GloveCols;
bool g_DbReady;
bool g_ProbeTried;
int g_WeaponColCount;
int g_ItemIdHigh = 16384;      // kgns bumps this so the client rebuilds the view model
char g_TeePath[PLATFORM_MAX_PATH];

// -2 = not attempted yet, 0 = unavailable, >0 = byte offset
int g_iProfileOff = -2;
int g_iTaskOff = -2;
int g_iMoraleOff = -2;
int g_iSafeOff = -2;
int g_iLookStateOff = -2;
int g_iNearOff = -2;
int g_iVisOff = -2;
int g_iDispOff = -2;
int g_iEnemyOff = -2;

/*  Per-client auto loadout, for 1v1s with friends: what to hand out at every
    spawn, whether to clear the rest of the guns first, and what to set the
    wallet to. -1 in g_Money means "leave the economy alone". These are globals
    rather than cvars because they are per-player; they survive rounds and map
    changes but not a plugin reload, which the tool re-applies from its own
    preset. */
char g_Give[MAXPLAYERS + 1][96];
bool g_Strip[MAXPLAYERS + 1];
int g_Money[MAXPLAYERS + 1];

public Plugin myinfo =
{
    name        = "LAN Skin Bridge",
    author      = "local tooling",
    description = "Apply + persist kgns weapon/glove skins from the server console or RCON",
    version     = PLUGIN_VERSION,
    url         = ""
};

public void OnPluginStart()
{
    RegAdminCmd("sm_lanfix_set",   Cmd_Set,    ADMFLAG_ROOT, "Apply+persist a skin: <target> <class> <paintkit> [seed] [trak] [wear]");
    RegAdminCmd("sm_lanfix_dump",  Cmd_Dump,   ADMFLAG_ROOT, "Print fallback props of a target's weapons and gloves");
    RegAdminCmd("sm_lanfix_gloves", Cmd_Gloves, ADMFLAG_ROOT, "Apply+persist gloves: <target> <tGroup> <tGlove> <tFloat> <ctGroup> <ctGlove> <ctFloat>");
    RegAdminCmd("sm_lanfix_dumpfile", Cmd_DumpFile, ADMFLAG_ROOT, "Write a target's weapon/glove props into data/lan_fix.log for external tooling");
    RegAdminCmd("sm_lanfix_botmem", Cmd_BotMem, ADMFLAG_ROOT, "Read back a bot's live BotProfile floats: <target>|all");
    RegAdminCmd("sm_lanfix_auto", Cmd_Auto, ADMFLAG_ROOT, "Weapons to hand out every spawn: <target> <weapon,weapon|off>");
    RegAdminCmd("sm_lanfix_strip", Cmd_StripCmd, ADMFLAG_ROOT, "Clear the other guns before the auto-give: <target> <on|off>");
    RegAdminCmd("sm_lanfix_money", Cmd_MoneyCmd, ADMFLAG_ROOT, "Set the wallet at every spawn: <target> <amount|off>");
    RegAdminCmd("sm_lanfix_autoshow", Cmd_AutoShow, ADMFLAG_ROOT, "Print the auto loadout state for every slot");
    RegAdminCmd("sm_lanfix_autoapply", Cmd_AutoApply, ADMFLAG_ROOT, "Run the auto loadout on a target right now, without waiting for a spawn");

    HookEvent("player_spawn", OnPlayerSpawn);   // post by default: the player is in the world and can hold guns

    /*  g_Money's "off" value is -1, but a reloaded plugin's globals start at 0
        and OnClientPutInServer never fires again for the players already in the
        game -- without this loop every connected bot would be forced to 0 cash
        at its next spawn. Observed exactly that on the first 1.2.0 test. */
    for (int i = 0; i <= MAXPLAYERS; i++)
        ResetAuto(i);

    BuildPath(Path_SM, g_TeePath, sizeof(g_TeePath), "data/lan_fix.log");

    char boot[64];
    FormatEx(boot, sizeof(boot), "lan_fix %s loaded", PLUGIN_VERSION);
    Tee(boot);

    g_WeaponCols = new StringMap();
    g_GloveCols = new StringMap();
    g_ConnName = FindConVar("sm_weapons_db_connection");
}

public void OnAllPluginsLoaded()
{
    // weapons.smx registers sm_weapons_db_connection; if this plugin started
    // first the cvar was not there yet, so resolve it and probe once more.
    if (g_ConnName == null)
        g_ConnName = FindConVar("sm_weapons_db_connection");
    if (!g_DbReady && !g_ProbeTried)
        ProbeSchema();
}

public void OnConfigsExecuted()
{
    if (!g_DbReady && !g_ProbeTried)
        ProbeSchema();
}

// ------------------------------------------------------------------ database

static void ProbeSchema()
{
    g_ProbeTried = true;
    if (g_ConnName == null)
    {
        LogMessage("lan_fix: sm_weapons_db_connection not found; live application only");
        return;
    }

    char conn[64], err[256];
    g_ConnName.GetString(conn, sizeof(conn));
    Database db = SQL_Connect(conn, false, err, sizeof(err));
    if (db == null)
    {
        LogMessage("lan_fix: cannot open \"%s\" (%s); live application only, nothing persists", conn, err);
        return;
    }
    db.Query(CbWeaponCols, "PRAGMA table_info(weapons)", false);
    db.Query(CbGloveCols, "PRAGMA table_info(gloves)", true);
    delete db;
}

public void CbWeaponCols(Database db, DBResultSet results, const char[] error, any data)
{
    HandlePack(db, results, error, data);
}

public void CbGloveCols(Database db, DBResultSet results, const char[] error, any data)
{
    HandlePack(db, results, error, data);
}

static void HandlePack(Database db, DBResultSet results, const char[] error, any gloves)
{
    delete db;
    if (results == null)
    {
        LogMessage("lan_fix: schema probe failed: %s", error);
        return;
    }
    StringMap map = gloves ? g_GloveCols : g_WeaponCols;
    while (results.FetchRow())
    {
        char col[64];
        results.FetchString(1, col, sizeof(col));   // PRAGMA field 1 is the column name
        map.SetString(col, "1");
        if (!gloves)
            g_WeaponColCount++;
    }
    if (!gloves)
        g_DbReady = g_WeaponColCount > 0;
}

static void WriteQuery(const char[] query)
{
    if (g_ConnName == null)
        return;
    char conn[64], err[256];
    g_ConnName.GetString(conn, sizeof(conn));
    Database db = SQL_Connect(conn, false, err, sizeof(err));
    if (db == null)
        return;
    db.Query(CbWrite, query, false);
    delete db;
}

public void CbWrite(Database db, DBResultSet results, const char[] error, any data)
{
    delete db;
    if (error[0] != '\0')
        LogMessage("lan_fix write: %s", error);
}

static bool HasCol(StringMap map, const char[] col)
{
    char v[4];
    return map.GetString(col, v, sizeof(v));
}

static void AddSet(char[] sets, int max, const char[] col, const char[] value)
{
    if (sets[0] != '\0')
        StrCat(sets, max, ", ");
    Format(sets, max, "%s%s=%s", sets, col, value);
}

/*  Returns true when the selection was written to the database.
    The plain "knife" column is kgns' knife MODEL selector, not a paintkit; the
    paintkit lives in knife_<model> (knife_karambit, knife_m9_bayonet, ...).
    Writing a paintkit into "knife" would corrupt the model choice on reconnect,
    so that name is refused here and callers must pass knife_<model>. */
static bool PersistWeapon(int client, const char[] cls, int paintkit, int seed, int trak, float wear)
{
    if (StrEqual(cls, "knife", false))
        return false;

    if (!g_DbReady)
        return false;

    char steamid[32];
    if (!GetClientAuthId(client, AuthId_Steam2, steamid, sizeof(steamid), true))
        return false;

    char col[72], val[32], sets[384];
    sets[0] = '\0';

    FormatEx(col, sizeof(col), "%s", cls);
    if (!HasCol(g_WeaponCols, col))
        return false;                           // unknown class: nothing to persist
    FormatEx(val, sizeof(val), "%d", paintkit);
    AddSet(sets, sizeof(sets), col, val);

    FormatEx(col, sizeof(col), "%s_seed", cls);
    if (seed >= -1 && HasCol(g_WeaponCols, col))
    {
        FormatEx(val, sizeof(val), "%d", seed);
        AddSet(sets, sizeof(sets), col, val);
    }
    FormatEx(col, sizeof(col), "%s_trak", cls);
    if (HasCol(g_WeaponCols, col))
    {
        FormatEx(val, sizeof(val), "%d", trak > 0 ? 1 : 0);
        AddSet(sets, sizeof(sets), col, val);
    }
    FormatEx(col, sizeof(col), "%s_trak_count", cls);
    if (HasCol(g_WeaponCols, col))
    {
        FormatEx(val, sizeof(val), "%d", trak > 1 ? trak : 0);
        AddSet(sets, sizeof(sets), col, val);
    }
    FormatEx(col, sizeof(col), "%s_float", cls);
    if (wear >= 0.0 && HasCol(g_WeaponCols, col))
    {
        FormatEx(val, sizeof(val), "%.6f", wear);
        AddSet(sets, sizeof(sets), col, val);
    }

    char q[512];
    FormatEx(q, sizeof(q), "UPDATE weapons SET %s WHERE steamid='%s'", sets, steamid);
    WriteQuery(q);
    return true;
}

static void PersistGloves(int client, int tGroup, int tGlove, float tFloat, int ctGroup, int ctGlove, float ctFloat)
{
    if (!g_DbReady)
        return;

    char steamid[32];
    if (!GetClientAuthId(client, AuthId_Steam2, steamid, sizeof(steamid), true))
        return;

    char col[32], val[32], sets[384];
    sets[0] = '\0';

    strcopy(col, sizeof(col), "t_group");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%d", tGroup); AddSet(sets, sizeof(sets), col, val); }
    strcopy(col, sizeof(col), "t_glove");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%d", tGlove); AddSet(sets, sizeof(sets), col, val); }
    strcopy(col, sizeof(col), "t_float");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%.6f", tFloat); AddSet(sets, sizeof(sets), col, val); }
    strcopy(col, sizeof(col), "ct_group");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%d", ctGroup); AddSet(sets, sizeof(sets), col, val); }
    strcopy(col, sizeof(col), "ct_glove");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%d", ctGlove); AddSet(sets, sizeof(sets), col, val); }
    strcopy(col, sizeof(col), "ct_float");
    if (HasCol(g_GloveCols, col)) { FormatEx(val, sizeof(val), "%.6f", ctFloat); AddSet(sets, sizeof(sets), col, val); }
    if (sets[0] == '\0')
        return;

    char q[512];
    FormatEx(q, sizeof(q), "INSERT OR IGNORE INTO gloves (steamid) VALUES ('%s')", steamid);
    WriteQuery(q);
    FormatEx(q, sizeof(q), "UPDATE gloves SET %s WHERE steamid='%s'", sets, steamid);
    WriteQuery(q);
}

// ------------------------------------------------------------------ targets

static void LowerCase(const char[] src, char[] dst, int max)
{
    int i = 0;
    for (; i < max - 1 && src[i] != '\0'; i++)
        dst[i] = (src[i] >= 'A' && src[i] <= 'Z') ? src[i] + 32 : src[i];
    dst[i] = '\0';
}

/*  A bare argument is tried in this order: userid, then player name
    (case-insensitive), then SteamID, then account id, and finally the exact
    client index. Note that "idx:" / "uid:" prefixes do NOT work when the
    command arrives through ServerCommand: the colon is consumed by the command
    tokenizer, so the plugin only ever sees "idx". For bots prefer their name --
    they report account id 0 and their userid differs from their index. */
static int ResolveTarget(const char[] arg)
{
    if (arg[0] == '\0')
        return 0;

    char lower[64];
    LowerCase(arg, lower, sizeof(lower));

    int uid = StringToInt(arg);
    if (uid > 0 && uid <= 65535)
    {
        int c = GetClientOfUserId(uid);
        if (c != 0 && IsClientInGame(c))
            return c;
    }

    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i))
            continue;

        char name[MAX_NAME_LENGTH];
        GetClientName(i, name, sizeof(name));
        LowerCase(name, name, sizeof(name));
        if (StrEqual(lower, name))
            return i;

        char id[32];
        if (GetClientAuthId(i, AuthId_Steam2, id, sizeof(id)) && StrEqual(lower, id))
            return i;
        if (GetClientAuthId(i, AuthId_SteamID64, id, sizeof(id)) && StrEqual(lower, id))
            return i;
    }

    if (uid > 0)
    {
        for (int i = 1; i <= MaxClients; i++)
        {
            if (IsClientInGame(i) && GetSteamAccountID(i) == uid)
                return i;
        }
        // final fallback: the number was meant as a client index
        if (uid <= MaxClients && IsClientInGame(uid))
            return uid;
    }
    return 0;
}

// ---------------------------------------------------------------- net props

static float ClampWear(float wear)
{
    if (wear <= 0.0)
        return 0.000001;
    if (wear >= 1.0)
        return 0.999999;
    return wear;
}

static void ApplyWeaponProps(int client, int ent, int paintkit, int seed, int trak, float wear)
{
    char cls[48];
    GetEntityClassname(ent, cls, sizeof(cls));
    bool knife = StrContains(cls, "knife", false) != -1;

    SetEntProp(ent, Prop_Send, "m_iItemIDLow", -1);
    g_ItemIdHigh++;
    SetEntProp(ent, Prop_Send, "m_iItemIDHigh", g_ItemIdHigh);
    SetEntProp(ent, Prop_Send, "m_nFallbackPaintKit", paintkit);
    // wear < 0 means the caller did not choose a float: leave the entity's
    // existing value alone instead of snapping it to Factory New.
    if (wear >= 0.0)
        SetEntPropFloat(ent, Prop_Send, "m_flFallbackWear", ClampWear(wear));
    SetEntProp(ent, Prop_Send, "m_nFallbackSeed", seed >= 0 ? seed : GetRandomInt(0, 8192));
    SetEntProp(ent, Prop_Send, "m_nFallbackStatTrak", trak > 0 ? trak : -1);
    SetEntProp(ent, Prop_Send, "m_iEntityQuality", knife ? 3 : (trak > 0 ? 9 : 0));
    SetEntProp(ent, Prop_Send, "m_iAccountID", GetSteamAccountID(client));
    SetEntPropEnt(ent, Prop_Send, "m_hOwnerEntity", client);
    SetEntPropEnt(ent, Prop_Send, "m_hPrevOwner", -1);
}

static int FindWeaponOfClass(int client, const char[] cls)
{
    char want[48];
    FormatEx(want, sizeof(want), "weapon_%s", cls);
    int total = GetEntPropArraySize(client, Prop_Send, "m_hMyWeapons");
    for (int i = 0; i < total; i++)
    {
        int ent = GetEntPropEnt(client, Prop_Send, "m_hMyWeapons", i);
        if (ent == -1 || !IsValidEntity(ent))
            continue;
        char found[48];
        GetEntityClassname(ent, found, sizeof(found));
        if (StrEqual(want, found, false))
            return ent;
        if (StrEqual(cls, "knife", false) && StrEqual(found, "weapon_knife", false))
            return ent;
    }
    return -1;
}

static int FindGloveWearable(int client)
{
    int ent = GetEntPropEnt(client, Prop_Send, "m_hMyWearables");
    return (ent != -1 && IsValidEntity(ent)) ? ent : -1;
}

static bool ApplyGloveProps(int client, int group, int glove, float wear)
{
    int ent = FindGloveWearable(client);
    if (ent == -1)
        return false;

    SetEntProp(ent, Prop_Send, "m_iItemIDLow", -1);
    g_ItemIdHigh++;
    SetEntProp(ent, Prop_Send, "m_iItemIDHigh", g_ItemIdHigh);
    if (group > 0)
        SetEntProp(ent, Prop_Send, "m_iItemDefinitionIndex", group);
    SetEntProp(ent, Prop_Send, "m_nFallbackPaintKit", glove);
    SetEntPropFloat(ent, Prop_Send, "m_flFallbackWear", ClampWear(wear));
    SetEntPropEnt(ent, Prop_Data, "m_hOwnerEntity", client);
    SetEntPropEnt(ent, Prop_Data, "m_hParent", client);
    SetEntPropEnt(ent, Prop_Data, "m_hMoveParent", client);
    SetEntProp(ent, Prop_Send, "m_bInitialized", 1);
    SetEntPropEnt(client, Prop_Send, "m_hMyWearables", ent);
    SetEntProp(client, Prop_Send, "m_nBody", 1);
    return true;
}

// ------------------------------------------------------------------ commands

public Action Cmd_Set(int client, int args)
{
    if (args < 3)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_set <target> <class> <paintkit> [seed] [trak] [wear]");
        return Plugin_Handled;
    }

    char aTarget[64], aClass[48], a3[32], a4[32], a5[32], a6[32];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    GetCmdArg(2, aClass, sizeof(aClass));
    GetCmdArg(3, a3, sizeof(a3));
    GetCmdArg(4, a4, sizeof(a4));
    GetCmdArg(5, a5, sizeof(a5));
    GetCmdArg(6, a6, sizeof(a6));

    int target = ResolveTarget(aTarget);
    if (target == 0)
    {
        ReplyToCommand(client, "[lan_fix] target not found: %s", aTarget);
        return Plugin_Handled;
    }

    int paintkit = StringToInt(a3);
    int seed = args >= 4 ? StringToInt(a4) : -1;
    int trak = args >= 5 ? StringToInt(a5) : 0;
    float wear = args >= 6 ? StringToFloat(a6) : 0.0;

    bool persisted = PersistWeapon(target, aClass, paintkit, seed, trak, wear);

    // knife_<model> columns still paint the one equipped knife entity
    char look[48];
    if (StrContains(aClass, "knife_", false) == 0)
        strcopy(look, sizeof(look), "knife");
    else
        strcopy(look, sizeof(look), aClass);

    int ent = FindWeaponOfClass(target, look);
    if (ent == -1)
    {
        char m2[224];
        FormatEx(m2, sizeof(m2), "[lan_fix] %s carries no %s: %s", aTarget, aClass, persisted ? "persisted, buy it and re-run to apply now" : "NOT persisted (no such column, or use knife_<model>)");
        ReplyToCommand(client, "%s", m2);
        Tee(m2);
        return Plugin_Handled;
    }

    ApplyWeaponProps(target, ent, paintkit, seed, trak, wear);

    char cls[48];
    GetEntityClassname(ent, cls, sizeof(cls));
    char msg[256];
    FormatEx(msg, sizeof(msg), "[lan_fix] %s -> %s paintkit=%d wear=%.6f seed=%d trak=%d %s",
        aTarget, cls, paintkit, ClampWear(wear), seed, trak,
        persisted ? "(persisted)" : (g_DbReady ? "(live only: use knife_<model> to persist a knife)" : "(live only: db unavailable)"));
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

public Action Cmd_Dump(int client, int args)
{
    if (args < 1)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_dump <target>");
        return Plugin_Handled;
    }

    char aTarget[64];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    int target = ResolveTarget(aTarget);
    if (target == 0)
    {
        ReplyToCommand(client, "[lan_fix] target not found: %s", aTarget);
        return Plugin_Handled;
    }

    int total = GetEntPropArraySize(target, Prop_Send, "m_hMyWeapons");
    ReplyToCommand(client, "[lan_fix] %s carries %d of %d slots", aTarget, WeaponCount(target), total);
    for (int i = 0; i < total; i++)
    {
        int ent = GetEntPropEnt(target, Prop_Send, "m_hMyWeapons", i);
        if (ent == -1 || !IsValidEntity(ent))
            continue;
        char cls[48];
        GetEntityClassname(ent, cls, sizeof(cls));
        ReplyToCommand(client, "  %-22s index=%-4d paintkit=%-5d wear=%.6f seed=%-5d trak=%-3d quality=%d",
            cls,
            GetEntProp(ent, Prop_Send, "m_iItemDefinitionIndex"),
            GetEntProp(ent, Prop_Send, "m_nFallbackPaintKit"),
            GetEntPropFloat(ent, Prop_Send, "m_flFallbackWear"),
            GetEntProp(ent, Prop_Send, "m_nFallbackSeed"),
            GetEntProp(ent, Prop_Send, "m_nFallbackStatTrak"),
            GetEntProp(ent, Prop_Send, "m_iEntityQuality"));
    }

    int glove = FindGloveWearable(target);
    if (glove != -1)
    {
        char gcls[48];
        GetEntityClassname(glove, gcls, sizeof(gcls));
        ReplyToCommand(client, "  wearable %s group=%d paintkit=%d wear=%.6f",
            gcls,
            GetEntProp(glove, Prop_Send, "m_iItemDefinitionIndex"),
            GetEntProp(glove, Prop_Send, "m_nFallbackPaintKit"),
            GetEntPropFloat(glove, Prop_Send, "m_flFallbackWear"));
    }
    else
        ReplyToCommand(client, "  no glove wearable");

    return Plugin_Handled;
}

public Action Cmd_Gloves(int client, int args)
{
    if (args < 7)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_gloves <target> <tGroup> <tGlove> <tFloat> <ctGroup> <ctGlove> <ctFloat>");
        return Plugin_Handled;
    }

    char aTarget[64], a2[24], a3[24], a4[24], a5[24], a6[24], a7[24];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    GetCmdArg(2, a2, sizeof(a2));
    GetCmdArg(3, a3, sizeof(a3));
    GetCmdArg(4, a4, sizeof(a4));
    GetCmdArg(5, a5, sizeof(a5));
    GetCmdArg(6, a6, sizeof(a6));
    GetCmdArg(7, a7, sizeof(a7));

    int target = ResolveTarget(aTarget);
    if (target == 0)
    {
        ReplyToCommand(client, "[lan_fix] target not found: %s", aTarget);
        return Plugin_Handled;
    }

    int tGroup = StringToInt(a2);
    int tGlove = StringToInt(a3);
    float tFloat = StringToFloat(a4);
    int ctGroup = StringToInt(a5);
    int ctGlove = StringToInt(a6);
    float ctFloat = StringToFloat(a7);

    bool ct = GetClientTeam(target) != 2;
    int group = ct ? ctGroup : tGroup;
    int glove = ct ? ctGlove : tGlove;
    float wear = ct ? ctFloat : tFloat;

    PersistGloves(target, tGroup, tGlove, tFloat, ctGroup, ctGlove, ctFloat);

    if (glove <= 0)
    {
        ReplyToCommand(client, "[lan_fix] no glove paintkit for team %d: persisted", GetClientTeam(target));
        return Plugin_Handled;
    }
    if (!ApplyGloveProps(target, group, glove, wear))
    {
        ReplyToCommand(client, "[lan_fix] no glove wearable yet (kgns creates it on spawn): persisted, respawn to apply");
        return Plugin_Handled;
    }

    char msg[256];
    FormatEx(msg, sizeof(msg), "[lan_fix] gloves group=%d paintkit=%d wear=%.6f %s", group, glove, ClampWear(wear), g_DbReady ? "(persisted)" : "(live only)");
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

// ------------------------------------------------------------------ tee

/*  Append one line to data/lan_fix.log so an external tool can read back what
    actually landed on the entities. ReplyToCommand alone is invisible to it:
    with no connectionless rcon in this build there is no console to scrape. */
static void Tee(const char[] msg)
{
    File h = OpenFile(g_TeePath, "at");
    if (h == null)
        return;
    char stamp[24];
    FormatTime(stamp, sizeof(stamp), "%H:%M:%S");
    h.WriteLine("[%s] %s", stamp, msg);
    delete h;
}

static int WeaponCount(int client)
{
    // m_hMyWeapons is a fixed 64-slot array; GetEntPropArraySize reports that
    // capacity, which read like a player carrying 64 guns. Count the filled slots.
    int total = GetEntPropArraySize(client, Prop_Send, "m_hMyWeapons");
    int filled = 0;
    for (int i = 0; i < total; i++)
    {
        int ent = GetEntPropEnt(client, Prop_Send, "m_hMyWeapons", i);
        if (ent != -1 && IsValidEntity(ent))
            filled++;
    }
    return filled;
}

static void BuildReport(int target, const char[] aTarget, char[] out, int maxlen)
{
    int total = GetEntPropArraySize(target, Prop_Send, "m_hMyWeapons");
    FormatEx(out, maxlen, "target=%s weapons=%d", aTarget, WeaponCount(target));

    char line[192];
    for (int i = 0; i < total; i++)
    {
        int ent = GetEntPropEnt(target, Prop_Send, "m_hMyWeapons", i);
        if (ent == -1 || !IsValidEntity(ent))
            continue;
        char cls[48];
        GetEntityClassname(ent, cls, sizeof(cls));
        FormatEx(line, sizeof(line),
            " | %s index=%d paintkit=%d wear=%.6f seed=%d trak=%d quality=%d",
            cls,
            GetEntProp(ent, Prop_Send, "m_iItemDefinitionIndex"),
            GetEntProp(ent, Prop_Send, "m_nFallbackPaintKit"),
            GetEntPropFloat(ent, Prop_Send, "m_flFallbackWear"),
            GetEntProp(ent, Prop_Send, "m_nFallbackSeed"),
            GetEntProp(ent, Prop_Send, "m_nFallbackStatTrak"),
            GetEntProp(ent, Prop_Send, "m_iEntityQuality"));
        StrCat(out, maxlen, line);
    }

    int glove = FindGloveWearable(target);
    if (glove != -1)
    {
        FormatEx(line, sizeof(line),
            " | glove paintkit=%d wear=%.6f group=%d",
            GetEntProp(glove, Prop_Send, "m_nFallbackPaintKit"),
            GetEntPropFloat(glove, Prop_Send, "m_flFallbackWear"),
            GetEntProp(glove, Prop_Send, "m_iItemDefinitionIndex"));
        StrCat(out, maxlen, line);
    }
    else
        StrCat(out, maxlen, " | glove=none");
}

public Action Cmd_DumpFile(int client, int args)
{
    if (args < 1)
    {
        char m0[96];
        FormatEx(m0, sizeof(m0), "[lan_fix] usage: sm_lanfix_dumpfile <target>");
        ReplyToCommand(client, "%s", m0);
        Tee(m0);
        return Plugin_Handled;
    }

    char aTarget[64];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    int target = ResolveTarget(aTarget);
    if (target == 0)
    {
        char m1[160];
        FormatEx(m1, sizeof(m1), "[lan_fix] dumpfile: target not found: %s", aTarget);
        ReplyToCommand(client, "%s", m1);
        Tee(m1);
        return Plugin_Handled;
    }

    static char report[1536];
    BuildReport(target, aTarget, report, sizeof(report));
    Tee(report);
    ReplyToCommand(client, "[lan_fix] dumped %s to data/lan_fix.log", aTarget);
    return Plugin_Handled;
}

// ------------------------------------------------------------------ auto loadout

static void ResetAuto(int client)
{
    g_Give[client][0] = '\0';
    g_Strip[client] = false;
    g_Money[client] = -1;
}

public void OnClientPutInServer(int client)
{
    ResetAuto(client);
}

public void OnClientDisconnect_Post(int client)
{
    ResetAuto(client);
}

/*  Expand a command's first argument into real slots. Besides one target
    (name / userid / account id, via ResolveTarget) it understands all, * ,
    humans and bots, which is what makes "give both players an awp" one line. */
static int ForEachTarget(const char[] arg, int[] out, int max)
{
    int n = 0;
    bool multi = StrEqual(arg, "all", false) || StrEqual(arg, "*")
        || StrEqual(arg, "humans", false) || StrEqual(arg, "bots", false);

    if (!multi)
    {
        int one = ResolveTarget(arg);
        if (one != 0)
            out[n++] = one;
        return n;
    }
    bool humans = StrEqual(arg, "all", false) || StrEqual(arg, "*") || StrEqual(arg, "humans", false);
    bool bots = StrEqual(arg, "all", false) || StrEqual(arg, "*") || StrEqual(arg, "bots", false);
    for (int i = 1; i <= MaxClients && n < max; i++)
    {
        if (!IsClientInGame(i))
            continue;
        if ((IsFakeClient(i) && bots) || (!IsFakeClient(i) && humans))
            out[n++] = i;
    }
    return n;
}

public Action OnPlayerSpawn(Event event, const char[] name, bool dontBroadcast)
{
    int client = GetClientOfUserId(event.GetInt("userid"));
    if (client < 1 || client > MaxClients || !IsClientInGame(client) || GetClientTeam(client) < 2)
        return Plugin_Continue;
    ApplyLoadout(client);
    return Plugin_Continue;
}

static void ApplyLoadout(int client)
{
    if (g_Strip[client])
    {
        // slots are primary/secondary/knife/grenade/C4; the knife stays so a
        // failed give can never leave a player holding nothing
        for (int slot = 0; slot <= 4; slot++)
        {
            if (slot == 2)
                continue;
            int ent = GetPlayerWeaponSlot(client, slot);
            while (ent != -1)
            {
                RemovePlayerItem(client, ent);
                RemoveEntity(ent);
                ent = GetPlayerWeaponSlot(client, slot);
            }
        }
    }

    if (g_Give[client][0] != '\0')
    {
        char parts[8][32];
        int n = ExplodeString(g_Give[client], ",", parts, 8, 32);
        for (int i = 0; i < n; i++)
        {
            TrimString(parts[i]);
            if (parts[i][0] == '\0')
                continue;
            char cls[48];
            if (StrContains(parts[i], "weapon_", false) == 0)
                strcopy(cls, sizeof(cls), parts[i]);
            else
                FormatEx(cls, sizeof(cls), "weapon_%s", parts[i]);

            int ent = GivePlayerItem(client, cls);
            if (ent == -1)
            {
                char nm[MAX_NAME_LENGTH];
                GetClientName(client, nm, sizeof(nm));
                char m[128];
                FormatEx(m, sizeof(m), "[lan_fix] %s could not be given %s (not a weapon this build knows)", nm, cls);
                Tee(m);
                continue;
            }
            EquipPlayerWeapon(client, ent);   // the last one listed ends up equipped
        }
    }

    if (g_Money[client] >= 0)
        SetEntProp(client, Prop_Send, "m_iAccount", g_Money[client]);
}

public Action Cmd_Auto(int client, int args)
{
    if (args < 2)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_auto <target>|all|humans|bots <weapon,weapon|off>");
        return Plugin_Handled;
    }
    char aTarget[64], aList[96];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    GetCmdArg(2, aList, sizeof(aList));

    int targets[MAXPLAYERS];
    int n = ForEachTarget(aTarget, targets, MAXPLAYERS);
    if (n == 0)
    {
        ReplyToCommand(client, "[lan_fix] auto: no target matched %s", aTarget);
        return Plugin_Handled;
    }
    bool off = StrEqual(aList, "off", false) || StrEqual(aList, "none", false);
    for (int i = 0; i < n; i++)
    {
        if (off)
            g_Give[targets[i]][0] = '\0';
        else
            strcopy(g_Give[targets[i]], sizeof(g_Give[]), aList);
    }
    char msg[192];
    FormatEx(msg, sizeof(msg), "[lan_fix] auto-give %s on %d player(s) (strip=%s money=%d)",
        off ? "off" : aList, n, g_Strip[targets[0]] ? "on" : "off", g_Money[targets[0]]);
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

public Action Cmd_StripCmd(int client, int args)
{
    if (args < 2)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_strip <target>|all|humans|bots <on|off>");
        return Plugin_Handled;
    }
    char aTarget[64], aOn[16];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    GetCmdArg(2, aOn, sizeof(aOn));

    int targets[MAXPLAYERS];
    int n = ForEachTarget(aTarget, targets, MAXPLAYERS);
    if (n == 0)
    {
        ReplyToCommand(client, "[lan_fix] strip: no target matched %s", aTarget);
        return Plugin_Handled;
    }
    bool on = StrEqual(aOn, "on", false) || StrEqual(aOn, "1", false) || StrEqual(aOn, "true", false);
    for (int i = 0; i < n; i++)
        g_Strip[targets[i]] = on;

    char msg[128];
    FormatEx(msg, sizeof(msg), "[lan_fix] strip=%s on %d player(s)", on ? "on" : "off", n);
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

public Action Cmd_MoneyCmd(int client, int args)
{
    if (args < 2)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_money <target>|all|humans|bots <amount|off>");
        return Plugin_Handled;
    }
    char aTarget[64], aAmt[16];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    GetCmdArg(2, aAmt, sizeof(aAmt));

    int targets[MAXPLAYERS];
    int n = ForEachTarget(aTarget, targets, MAXPLAYERS);
    if (n == 0)
    {
        ReplyToCommand(client, "[lan_fix] money: no target matched %s", aTarget);
        return Plugin_Handled;
    }
    bool off = StrEqual(aAmt, "off", false) || StrEqual(aAmt, "none", false);
    int amount = 0;
    if (!off)
    {
        amount = StringToInt(aAmt);
        if (amount < 0)
            amount = 0;
        if (amount > 32000)
            amount = 32000;
    }
    for (int i = 0; i < n; i++)
        g_Money[targets[i]] = off ? -1 : amount;

    char msg[128];
    FormatEx(msg, sizeof(msg), "[lan_fix] money at spawn = %s on %d player(s)", off ? "off" : aAmt, n);
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

public Action Cmd_AutoShow(int client, int args)
{
    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i))
            continue;
        if (g_Give[i][0] == '\0' && !g_Strip[i] && g_Money[i] < 0)
            continue;
        char nm[MAX_NAME_LENGTH];
        GetClientName(i, nm, sizeof(nm));
        char msg[224];
        FormatEx(msg, sizeof(msg), "autoset idx=%d name=%s bot=%d give=%s strip=%d money=%d account=%d alive=%d",
            i, nm, IsFakeClient(i) ? 1 : 0, g_Give[i][0] != '\0' ? g_Give[i] : "-",
            g_Strip[i] ? 1 : 0, g_Money[i], GetEntProp(i, Prop_Send, "m_iAccount"),
            IsPlayerAlive(i) ? 1 : 0);
        ReplyToCommand(client, "%s", msg);
        Tee(msg);
    }
    ReplyToCommand(client, "[lan_fix] autoshow done");
    return Plugin_Handled;
}

/*  Same routine the spawn hook runs, on demand. Without this there is no way
    to check a loadout change other than by waiting for the next round, which
    makes the feature impossible to verify from the tool. */
public Action Cmd_AutoApply(int client, int args)
{
    if (args < 1)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_autoapply <target>|all|humans|bots");
        return Plugin_Handled;
    }
    char aTarget[64];
    GetCmdArg(1, aTarget, sizeof(aTarget));

    int targets[MAXPLAYERS];
    int n = ForEachTarget(aTarget, targets, MAXPLAYERS);
    if (n == 0)
    {
        ReplyToCommand(client, "[lan_fix] autoapply: no target matched %s", aTarget);
        return Plugin_Handled;
    }
    int done = 0;
    for (int i = 0; i < n; i++)
    {
        if (!IsPlayerAlive(targets[i]))
            continue;                       // a corpse holds nothing; its spawn will apply it
        ApplyLoadout(targets[i]);
        done++;
    }
    char msg[128];
    FormatEx(msg, sizeof(msg), "[lan_fix] auto loadout applied to %d of %d target(s)", done, n);
    ReplyToCommand(client, "%s", msg);
    Tee(msg);
    return Plugin_Handled;
}

// ------------------------------------------------------------------ bot probing

/*  bot_stuff.smx overwrites four floats inside the engine's BotProfile every
    tick, so botprofile.db alone only tells you what a bot is supposed to be,
    not what it is running as. These reads go through the exact same gamedata
    offset bot_stuff itself uses (CCSBot::m_pLocalProfile); nothing is
    hardcoded, and if the conf cannot be read the command says so instead of
    guessing at a number. */
static bool ResolveBotOffsets()
{
    if (g_iProfileOff != -2)
        return g_iProfileOff > 0;

    GameData gd = new GameData("botstuff.games");
    if (gd == null)
    {
        g_iProfileOff = 0;
        return false;
    }

    g_iProfileOff   = gd.GetOffset("CCSBot::m_pLocalProfile");
    g_iTaskOff      = gd.GetOffset("CCSBot::m_task");
    g_iMoraleOff    = gd.GetOffset("CCSBot::m_morale");
    g_iSafeOff      = gd.GetOffset("CCSBot::m_safeTime");
    g_iLookStateOff = gd.GetOffset("CCSBot::m_lookAtSpotState");
    g_iNearOff      = gd.GetOffset("CCSBot::m_nearbyEnemyCount");
    g_iVisOff       = gd.GetOffset("CCSBot::m_isEnemyVisible");
    g_iDispOff      = gd.GetOffset("CCSBot::m_disposition");
    g_iEnemyOff     = gd.GetOffset("CCSBot::m_enemy");
    delete gd;

    if (g_iProfileOff < 1)
        g_iProfileOff = 0;
    return g_iProfileOff > 0;
}

// -99999 marks "this offset did not resolve", so the tool prints n/a for it
static float ReadFloatAt(Address base, int off)
{
    if (off < 1)
        return -99999.0;
    return view_as<float>(LoadFromAddress(base + view_as<Address>(off), NumberType_Int32));
}

static int ReadIntAt(Address base, int off)
{
    if (off < 1)
        return -99999;
    return LoadFromAddress(base + view_as<Address>(off), NumberType_Int32);
}

static void HexWord(char[] out, int maxlen, int value)
{
    char w[9];
    FormatEx(w, sizeof(w), "%02x%02x%02x%02x",
        value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF);
    StrCat(out, maxlen, w);
}

/*  The CCSBot members in botstuff.games are offsets into the player entity
    itself, so they are read the way bot_stuff reads them: GetEntData. */
static int ReadEntInt(int ent, int off)
{
    if (off < 1)
        return -99999;
    return GetEntData(ent, off);
}

static float ReadEntFloat(int ent, int off)
{
    if (off < 1)
        return -99999.0;
    return view_as<float>(GetEntData(ent, off));
}

static void BuildBotReport(int idx, char[] out, int maxlen)
{
    char name[MAX_NAME_LENGTH];
    GetClientName(idx, name, sizeof(name));

    int raw = GetEntData(idx, g_iProfileOff);
    FormatEx(out, maxlen, "botmem idx=%d name=%s team=%d alive=%d profile=0x%08x",
        idx, name, GetClientTeam(idx), IsPlayerAlive(idx) ? 1 : 0, raw);

    // A slot that never got a profile holds 0; anything unaligned or outside
    // the 32-bit process heap is stale, and reading it would fault the server.
    if (raw < 0x01000000 || (raw & 3) != 0)
    {
        StrCat(out, maxlen, " SKIPPED (profile pointer not a plausible address)");
        return;
    }

    Address prof = view_as<Address>(raw);
    char line[192];

    /*  The four fields bot_stuff writes: +4 aggression, +84 reaction time,
        +104 / +116 look-angle max acceleration (normal / attacking).
        %.6f, not %g -- SourcePawn's Format has no %g and emits the literal
        letter, which is how "aggression=g" shipped for one build. The exact
        bits are in raw= below, so these labels only need to be readable. */
    FormatEx(line, sizeof(line), " aggression=%.6f reactionTime=%.6f accelNormal=%.6f accelAttacking=%.6f",
        ReadFloatAt(prof, 4), ReadFloatAt(prof, 84),
        ReadFloatAt(prof, 104), ReadFloatAt(prof, 116));
    StrCat(out, maxlen, line);

    StrCat(out, maxlen, " live=");
    FormatEx(line, sizeof(line), "task=%d morale=%d lookState=%d nearby=%d visible=%d disp=%d safe=%.6f enemy=0x%08x",
        ReadEntInt(idx, g_iTaskOff), ReadEntInt(idx, g_iMoraleOff),
        ReadEntInt(idx, g_iLookStateOff), ReadEntInt(idx, g_iNearOff),
        ReadEntInt(idx, g_iVisOff), ReadEntInt(idx, g_iDispOff),
        ReadEntFloat(idx, g_iSafeOff), ReadEntInt(idx, g_iEnemyOff));
    StrCat(out, maxlen, line);

    // 40 little-endian dwords of the profile struct: lets the tooling find the
    // fields nobody has documented by matching them against botprofile.db
    StrCat(out, maxlen, " raw=");
    for (int i = 0; i < 40; i++)
        HexWord(out, maxlen, ReadIntAt(prof, i * 4));
}

public Action Cmd_BotMem(int client, int args)
{
    if (args < 1)
    {
        ReplyToCommand(client, "[lan_fix] usage: sm_lanfix_botmem <target>|all");
        return Plugin_Handled;
    }

    if (!ResolveBotOffsets())
    {
        char m0[160];
        FormatEx(m0, sizeof(m0), "[lan_fix] botmem: CCSBot::m_pLocalProfile unavailable (gamedata/botstuff.games missing or unparsed)");
        ReplyToCommand(client, "%s", m0);
        Tee(m0);
        return Plugin_Handled;
    }

    char aTarget[64];
    GetCmdArg(1, aTarget, sizeof(aTarget));
    bool all = StrEqual(aTarget, "all", false) || StrEqual(aTarget, "*") || StrEqual(aTarget, "bots", false);

    int target = all ? 0 : ResolveTarget(aTarget);
    if (!all && target == 0)
    {
        char m1[160];
        FormatEx(m1, sizeof(m1), "[lan_fix] botmem: target not found: %s", aTarget);
        ReplyToCommand(client, "%s", m1);
        Tee(m1);
        return Plugin_Handled;
    }

    int shown = 0;
    static char report[4096];
    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i) || !IsFakeClient(i))
            continue;
        if (!all && i != target)
            continue;

        BuildBotReport(i, report, sizeof(report));
        Tee(report);
        ReplyToCommand(client, "%s", report);
        shown++;
    }

    if (shown == 0)
    {
        char m2[160];
        FormatEx(m2, sizeof(m2), "[lan_fix] botmem: %s is not a bot in this game", aTarget);
        ReplyToCommand(client, "%s", m2);
        Tee(m2);
    }
    return Plugin_Handled;
}
