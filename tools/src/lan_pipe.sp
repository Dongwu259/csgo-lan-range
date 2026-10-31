#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <cstrike>

#define PLUGIN_VERSION "1.3.0"

/*
    lan_pipe.sp  --  a file based command pipe for local tooling.

    Why a file and not RCON: this CS:GO build has no connectionless rcon. There
    is no "challenge rcon" handshake in bin/engine.dll at all; what exists is
    rcon_connected_clients_allow ("Allow clients to use rcon commands server")
    and the client-side error "rcon functionality disabled by server". So rcon
    only rides an established client connection, and the only client of a
    listen server is the game window itself. An external program therefore has
    no socket to talk to, while the tool and the game share this disk.

    Protocol, one entry per line in the pipe file:
        #comment / ;comment / blank   ignored
        ?<cvar>                       log that cvar's current value
        ~status                       log map, human/bot counts, timeleft, scores
        ~players                      log one line per connected slot
        anything else                 passed to ServerCommand() as the server console

    After a pass the pipe file is truncated, so each line runs exactly once.
    Everything is answered into the one result log the tool tails.

    This is by design a code-execution channel on local disk. Keep it off any
    machine you do not control, and use "sm_lanpipe_enable 0" to stop polling.
*/

ConVar g_Enabled;
ConVar g_Interval;
ConVar g_PipeFile;
ConVar g_LogFile;
Handle g_Timer;
bool g_MapActive;

public Plugin myinfo =
{
    name        = "LAN Command Pipe",
    author      = "local tooling",
    description = "Executes lines dropped into a local file as server console commands; answers state queries",
    version     = PLUGIN_VERSION,
    url         = ""
};

public void OnPluginStart()
{
    CreateConVar("sm_lanpipe_version", PLUGIN_VERSION, "LAN Command Pipe version", FCVAR_NOTIFY | FCVAR_DONTRECORD);
    g_Enabled  = CreateConVar("sm_lanpipe_enable", "1", "Poll the pipe file", FCVAR_NONE, true, 0.0, true, 1.0);
    g_Interval = CreateConVar("sm_lanpipe_interval", "0.2", "Seconds between pipe polls", FCVAR_NONE, true, 0.05, true, 5.0);
    g_PipeFile = CreateConVar("sm_lanpipe_file", "data/lan_pipe.txt", "Pipe file, relative to addons/sourcemod");
    g_LogFile  = CreateConVar("sm_lanpipe_log", "data/lan_exec.log", "Result log, relative to addons/sourcemod");

    // A reload that quietly kept the previous build is otherwise invisible from
    // outside, so stamp the version into the log the tool already reads.
    char boot[64];
    FormatEx(boot, sizeof(boot), "lan_pipe %s loaded", PLUGIN_VERSION);
    LogResult(boot);

    AutoExecConfig(true, "lan_pipe");
    HookConVarChange(g_Interval, OnIntervalChanged);

    g_MapActive = false;
    for (int i = 1; i <= MaxClients; i++)
    {
        if (IsClientInGame(i))
        {
            g_MapActive = true;
            break;
        }
    }
    if (g_MapActive)
        StartTimer();
}

public void OnMapStart()
{
    g_MapActive = true;
    StartTimer();
}

public void OnMapEnd()
{
    g_MapActive = false;
    if (g_Timer != null)
    {
        KillTimer(g_Timer);
        g_Timer = null;
    }
}

public void OnIntervalChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
    StartTimer();
}

static void StartTimer()
{
    if (g_Timer != null)
    {
        KillTimer(g_Timer);
        g_Timer = null;
    }
    float f = g_Interval.FloatValue;
    if (f < 0.05)
        f = 0.05;
    g_Timer = CreateTimer(f, PollPipe, _, TIMER_REPEAT);
}

public Action PollPipe(Handle timer)
{
    if (!g_MapActive || g_Enabled.IntValue == 0)
        return Plugin_Continue;

    char path[PLATFORM_MAX_PATH];
    g_PipeFile.GetString(path, sizeof(path));

    char full[PLATFORM_MAX_PATH];
    BuildPath(Path_SM, full, sizeof(full), path);

    if (!FileExists(full, true))
        return Plugin_Continue;

    File h = OpenFile(full, "rt");
    if (h == null)
        return Plugin_Continue;

    char line[512];
    int ran;
    while (!h.EndOfFile() && h.ReadLine(line, sizeof(line)))
    {
        TrimString(line);
        if (line[0] == '\0' || line[0] == '#' || line[0] == ';' || (line[0] == '/' && line[1] == '/'))
            continue;
        ExecuteLine(line);
        ran++;
    }
    delete h;

    if (ran > 0)
    {
        // truncate so nothing runs twice
        File w = OpenFile(full, "wt");
        if (w != null)
            delete w;
    }
    return Plugin_Continue;
}

static void ExecuteLine(const char[] line)
{
    char msg[544];

    if (line[0] == '~')
    {
        ReportState(line[1]);
        return;
    }

    if (line[0] == '?')
    {
        char name[128];
        strcopy(name, sizeof(name), line[1]);
        TrimString(name);

        ConVar cv = FindConVar(name);
        if (cv == null)
        {
            FormatEx(msg, sizeof(msg), "read %s = <no such cvar>", name);
            LogResult(msg);
            return;
        }
        char value[128];
        cv.GetString(value, sizeof(value));
        FormatEx(msg, sizeof(msg), "%s = %s", name, value);
        LogResult(msg);
        return;
    }

    FormatEx(msg, sizeof(msg), "exec: %s", line);
    LogResult(msg);
    ServerCommand("%s", line);
}

static void ReportState(const char[] what)
{
    char msg[256];

    if (StrEqual(what, "status", false))
    {
        char map[64];
        GetCurrentMap(map, sizeof(map));
        int humans;
        int bots;
        for (int i = 1; i <= MaxClients; i++)
        {
            if (!IsClientInGame(i))
                continue;
            if (IsFakeClient(i))
                bots++;
            else
                humans++;
        }

        int timeleft = -1;
        GetMapTimeLeft(timeleft);
        ConVar fr = FindConVar("mp_freezetime");

        FormatEx(msg, sizeof(msg), "status map=%s humans=%d bots=%d max=%d timeleft=%d freeze=%d t=%d ct=%d",
            map, humans, bots, MaxClients, timeleft,
            (fr != null) ? fr.IntValue : -1,
            CS_GetTeamScore(CS_TEAM_T), CS_GetTeamScore(CS_TEAM_CT));
        LogResult(msg);
        return;
    }

    if (StrEqual(what, "players", false))
    {
        for (int i = 1; i <= MaxClients; i++)
        {
            if (!IsClientInGame(i))
                continue;

            char name[MAX_NAME_LENGTH], safe[MAX_NAME_LENGTH * 2];
            GetClientName(i, name, sizeof(name));
            Sanitize(name, safe, sizeof(safe));

            FormatEx(msg, sizeof(msg), "player index=%d userid=%d account=%d name=\"%s\" team=%d alive=%d bot=%d",
                i, GetClientUserId(i), GetSteamAccountID(i), safe, GetClientTeam(i),
                IsPlayerAlive(i) ? 1 : 0, IsFakeClient(i) ? 1 : 0);
            LogResult(msg);
        }
        return;
    }

    FormatEx(msg, sizeof(msg), "unknown state query: ~%s", what);
    LogResult(msg);
}

/*  Player names are emitted quoted, so the only character that has to go is
    the double quote itself (plus control bytes). Dropping spaces too -- as an
    earlier version did -- silently mangled every name with a space, and the
    tool then could not match it against botprofile.db ("[RAM]The Admin" came
    back as [RAM]TheAdmin and read as an unknown bot tier). */
static void Sanitize(const char[] src, char[] dst, int maxlen)
{
    int j = 0;
    for (int i = 0; src[i] != '\0' && j < maxlen - 1; i++)
    {
        int c = src[i];
        if (c == '"')
            continue;
        if (c >= 0x80 || (c >= 0x20 && c < 0x7f))
            dst[j++] = src[i];
    }
    dst[j] = '\0';
}

static void LogResult(const char[] msg)
{
    // resolve every time so sm_lanpipe_log can be retargeted without a restart
    char rel[PLATFORM_MAX_PATH];
    g_LogFile.GetString(rel, sizeof(rel));
    char path[PLATFORM_MAX_PATH];
    BuildPath(Path_SM, path, sizeof(path), rel);

    File h = OpenFile(path, "at");
    if (h == null)
        return;
    char time[32];
    FormatTime(time, sizeof(time), "%H:%M:%S");
    h.WriteLine("[%s] %s", time, msg);
    delete h;
}
