//+------------------------------------------------------------------+
//|                                                  AtlasBridge.mq5 |
//|  Terminal-side implementation of the ATLAS bridge protocol v1.    |
//|  See docs/PROTOCOL.md for the contract this file implements.      |
//|                                                                   |
//|  WHAT THIS EA DOES NOT DO, deliberately:                          |
//|    - It has no strategy. It never decides to trade.               |
//|    - It never retries a failed order. Retry needs an idempotency  |
//|      check the engine owns (ADR-013); an EA that retries on its   |
//|      own can double fill.                                         |
//|    - It never touches a position whose magic is not ours.         |
//|                                                                   |
//|  Keeping the terminal side thin is the point. MQL5 cannot be      |
//|  meaningfully unit tested, so the untestable surface is kept as   |
//|  small as possible and everything that can be decided is decided  |
//|  in Python, where it is tested.                                   |
//|                                                                   |
//|  TRANSPORT: MQL5 provides OUTBOUND sockets only, so ATLAS listens |
//|  and this EA connects out. That is what removes the ZeroMQ DLL    |
//|  dependency -- no "Allow DLL imports", which would grant an EA    |
//|  unrestricted native code execution.                              |
//|                                                                   |
//|  SETUP:                                                           |
//|    1. Copy mql5/Include/Atlas/*.mqh to MQL5/Include/Atlas/        |
//|    2. Copy this file to MQL5/Experts/ and compile in MetaEditor   |
//|    3. Tools > Options > Expert Advisors: permit the ATLAS host    |
//|       (e.g. 127.0.0.1) in the allowed-addresses list              |
//|    4. Enable Algo Trading, attach to ONE chart of any symbol      |
//|    5. Start ATLAS first: it is the listener                       |
//+------------------------------------------------------------------+
#property copyright "ATLAS"
#property version   "1.00"
#property strict
#property description "Bridge between MetaTrader 5 and the ATLAS trading engine."
#property description "Carries no strategy: it executes what ATLAS asks and reports state."

#include <Trade/Trade.mqh>
#include <Atlas/Json.mqh>
#include <Atlas/Spec.mqh>
#include <Atlas/Orders.mqh>

//--- inputs ---------------------------------------------------------
input string   InpHost              = "127.0.0.1";   // ATLAS host
input int      InpPort              = 5555;          // ATLAS port
input string   InpToken             = "";            // shared secret (must match ATLAS)
input long     InpMagic             = 20260823;      // magic number this bridge owns
input string   InpSymbols           = "XAUUSD";      // comma-separated symbols to stream
input string   InpTimeframes        = "M5,M15,H1,H4";// timeframes to stream closed bars for
input int      InpTickThrottleMs    = 200;           // minimum gap between ticks per symbol
input int      InpReconnectSeconds  = 5;             // backoff base when the socket drops
input bool     InpVerboseLog        = false;         // log every frame (noisy; debugging only)

//--- state ----------------------------------------------------------
int      g_socket       = INVALID_HANDLE;
bool     g_hello_sent   = false;
bool     g_welcomed     = false;
string   g_rx_buffer    = "";
datetime g_next_connect = 0;
int      g_backoff      = 0;
CTrade   g_trade;

string            g_symbols[];
ENUM_TIMEFRAMES   g_timeframes[];
string            g_tf_names[];
datetime          g_last_bar_time[];   // [symbol_index * tf_count + tf_index]
long              g_last_tick_ms[];    // per symbol
long              g_frames_in  = 0;
long              g_frames_out = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(!ParseSymbols(InpSymbols) || ArraySize(g_symbols) == 0)
     {
      Print("AtlasBridge: no valid symbols in '", InpSymbols, "'");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!ParseTimeframes(InpTimeframes) || ArraySize(g_timeframes) == 0)
     {
      Print("AtlasBridge: no valid timeframes in '", InpTimeframes, "'");
      return INIT_PARAMETERS_INCORRECT;
     }

   int n = ArraySize(g_symbols) * ArraySize(g_timeframes);
   ArrayResize(g_last_bar_time, n);
   ArrayInitialize(g_last_bar_time, 0);
   ArrayResize(g_last_tick_ms, ArraySize(g_symbols));
   ArrayInitialize(g_last_tick_ms, 0);

   // Seed the last-known bar time for every stream. Without this the EA would emit the
   // current, still-forming bar as if it had just closed on the first tick after start.
   for(int s = 0; s < ArraySize(g_symbols); s++)
     {
      if(!SymbolSelect(g_symbols[s], true))
         Print("AtlasBridge: WARNING ", g_symbols[s], " could not be selected in Market Watch");
      for(int t = 0; t < ArraySize(g_timeframes); t++)
         g_last_bar_time[s * ArraySize(g_timeframes) + t] =
            iTime(g_symbols[s], g_timeframes[t], 0);
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   PrintFormat("AtlasBridge v1 starting. host=%s:%d magic=%I64d symbols=%d timeframes=%d",
               InpHost, InpPort, InpMagic, ArraySize(g_symbols), ArraySize(g_timeframes));
   PrintFormat("AtlasBridge: server offset from UTC = %I64d s", AtlasServerOffsetSeconds());
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("AtlasBridge: WARNING algorithmic trading is DISABLED for this EA; "
            "orders will be refused until it is enabled");

   EventSetMillisecondTimer(200);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_socket != INVALID_HANDLE)
     {
      SendFrame("{\"v\":1,\"t\":\"bye\",\"ts\":" + IntegerToString(AtlasUtcNowMs()) + "}");
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
     }
   PrintFormat("AtlasBridge stopped (reason %d). frames in=%I64d out=%I64d",
               reason, g_frames_in, g_frames_out);
  }

//+------------------------------------------------------------------+
//| The timer drives everything: connection management, reading, and  |
//| bar-close detection. OnTick only forwards quotes, because a       |
//| symbol we stream may not be the chart symbol and would therefore  |
//| never produce a tick event here.                                  |
//+------------------------------------------------------------------+
void OnTimer()
  {
   EnsureConnected();
   if(g_socket == INVALID_HANDLE)
      return;
   ReadIncoming();
   PublishQuotes();
   PublishClosedBars();
  }

void OnTick()
  {
   PublishQuotes();
  }

//+------------------------------------------------------------------+
//| Trade transactions are forwarded as HINTS. ATLAS re-reads         |
//| authoritative state rather than trusting them, because            |
//| transactions can arrive out of order and can be missed across a   |
//| reconnect.                                                        |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(g_socket == INVALID_HANDLE || !g_welcomed)
      return;
   string parts[];
   ArrayResize(parts, 12);
   int i = 0;
   parts[i++] = JsonStr("t", "txn");
   parts[i++] = JsonInt("v", 1);
   parts[i++] = JsonInt("ts", AtlasUtcNowMs());
   parts[i++] = JsonStr("type", EnumToString(trans.type));
   parts[i++] = JsonInt("deal", (long)trans.deal);
   parts[i++] = JsonInt("order", (long)trans.order);
   parts[i++] = JsonInt("position", (long)trans.position);
   parts[i++] = JsonStr("sym", trans.symbol);
   parts[i++] = JsonNum("volume", trans.volume, 4);
   parts[i++] = JsonNum("price", trans.price, 8);
   parts[i++] = JsonInt("retcode", (int)result.retcode);
   parts[i++] = JsonStr("comment", request.comment);
   SendFrame(JsonObject(parts, i));
  }

//+------------------------------------------------------------------+
//| Connection                                                        |
//+------------------------------------------------------------------+
void EnsureConnected()
  {
   if(g_socket != INVALID_HANDLE)
     {
      if(SocketIsConnected(g_socket))
         return;
      Print("AtlasBridge: connection lost");
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      g_hello_sent = false;
      g_welcomed = false;
      g_rx_buffer = "";
     }
   if(TimeCurrent() < g_next_connect)
      return;

   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE)
     {
      PrintFormat("AtlasBridge: SocketCreate failed, error %d. If this is 4014, socket "
                  "functions are not permitted -- add %s to the allowed addresses in "
                  "Tools > Options > Expert Advisors.", GetLastError(), InpHost);
      ScheduleReconnect();
      return;
     }
   if(!SocketConnect(g_socket, InpHost, InpPort, 1000))
     {
      PrintFormat("AtlasBridge: cannot reach ATLAS at %s:%d (error %d). Is the engine "
                  "running? ATLAS listens; the terminal connects out.",
                  InpHost, InpPort, GetLastError());
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      ScheduleReconnect();
      return;
     }
   g_backoff = 0;
   Print("AtlasBridge: connected to ATLAS");
   SendHello();
  }

void ScheduleReconnect()
  {
   // Exponential backoff, capped. A terminal that reconnects every 200 ms against a dead
   // engine produces a log nobody will read.
   g_backoff = (g_backoff == 0) ? InpReconnectSeconds : MathMin(g_backoff * 2, 60);
   g_next_connect = TimeCurrent() + g_backoff;
  }

void SendHello()
  {
   string symbols_json = "[";
   for(int i = 0; i < ArraySize(g_symbols); i++)
     {
      if(i > 0)
         symbols_json += ",";
      symbols_json += "\"" + JsonEscape(g_symbols[i]) + "\"";
     }
   symbols_json += "]";

   string parts[];
   ArrayResize(parts, 9);
   int i = 0;
   parts[i++] = JsonStr("t", "hello");
   parts[i++] = JsonInt("v", 1);
   parts[i++] = JsonInt("ts", AtlasUtcNowMs());
   parts[i++] = JsonStr("token", InpToken);
   parts[i++] = JsonStr("impl", "mql5-ea");
   parts[i++] = JsonInt("build", TerminalInfoInteger(TERMINAL_BUILD));
   parts[i++] = JsonInt("server_time_ms", (long)TimeCurrent() * 1000);
   parts[i++] = "\"account\":" + AccountJson();
   parts[i++] = "\"symbols\":" + symbols_json;
   SendFrame(JsonObject(parts, i));
   g_hello_sent = true;
  }

string AccountJson()
  {
   string parts[];
   ArrayResize(parts, 12);
   int i = 0;
   parts[i++] = JsonInt("login", AccountInfoInteger(ACCOUNT_LOGIN));
   parts[i++] = JsonStr("server", AccountInfoString(ACCOUNT_SERVER));
   parts[i++] = JsonStr("currency", AccountInfoString(ACCOUNT_CURRENCY));
   parts[i++] = JsonNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2);
   parts[i++] = JsonNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2);
   parts[i++] = JsonNum("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2);
   parts[i++] = JsonNum("free_margin", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   parts[i++] = JsonNum("margin_level", AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2);
   parts[i++] = JsonInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE));
   parts[i++] = JsonInt("ts", AtlasUtcNowMs());
   parts[i++] = JsonBool("trade_allowed",
                         AccountInfoInteger(ACCOUNT_TRADE_EXPERT) &&
                         MQLInfoInteger(MQL_TRADE_ALLOWED) &&
                         TerminalInfoInteger(TERMINAL_TRADE_ALLOWED));
   parts[i++] = JsonBool("hedging",
                         AccountInfoInteger(ACCOUNT_MARGIN_MODE) ==
                         ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   return JsonObject(parts, i);
  }

//+------------------------------------------------------------------+
//| Framing                                                           |
//+------------------------------------------------------------------+
bool SendFrame(const string frame)
  {
   if(g_socket == INVALID_HANDLE)
      return false;
   string line = frame + "\n";
   uchar bytes[];
   int len = StringToCharArray(line, bytes, 0, WHOLE_ARRAY, CP_UTF8) - 1;
   if(len <= 0)
      return false;
   if(SocketSend(g_socket, bytes, len) != len)
     {
      PrintFormat("AtlasBridge: SocketSend failed, error %d", GetLastError());
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      g_welcomed = false;
      ScheduleReconnect();
      return false;
     }
   g_frames_out++;
   if(InpVerboseLog)
      Print("-> ", frame);
   return true;
  }

void ReadIncoming()
  {
   uint available = SocketIsReadable(g_socket);
   while(available > 0)
     {
      uchar bytes[];
      int read = SocketRead(g_socket, bytes, (int)MathMin(available, 65536), 100);
      if(read <= 0)
         break;
      g_rx_buffer += CharArrayToString(bytes, 0, read, CP_UTF8);
      available = SocketIsReadable(g_socket);
     }

   int nl = StringFind(g_rx_buffer, "\n", 0);
   while(nl >= 0)
     {
      string frame = StringSubstr(g_rx_buffer, 0, nl);
      g_rx_buffer = StringSubstr(g_rx_buffer, nl + 1);
      StringTrimLeft(frame);
      StringTrimRight(frame);
      if(StringLen(frame) > 0)
        {
         g_frames_in++;
         if(InpVerboseLog)
            Print("<- ", frame);
         HandleFrame(frame);
        }
      nl = StringFind(g_rx_buffer, "\n", 0);
     }
   // A peer that never sends a newline must not be able to grow this buffer without bound.
   if(StringLen(g_rx_buffer) > 262144)
     {
      Print("AtlasBridge: receive buffer overflow, dropping connection");
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      g_rx_buffer = "";
      g_welcomed = false;
      ScheduleReconnect();
     }
  }

void HandleFrame(const string frame)
  {
   string kind = JsonGetString(frame, "t", "");
   if(kind == "welcome")
     {
      g_welcomed = true;
      Print("AtlasBridge: handshake accepted by ATLAS");
      return;
     }
   if(kind == "bye")
     {
      PrintFormat("AtlasBridge: ATLAS closed the session: %s",
                  JsonGetString(frame, "reason", ""));
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      g_welcomed = false;
      ScheduleReconnect();
      return;
     }
   if(kind == "ping")
     {
      SendFrame("{\"v\":1,\"t\":\"pong\",\"ts\":" + IntegerToString(AtlasUtcNowMs()) +
                ",\"id\":\"" + JsonEscape(JsonGetString(frame, "id", "")) + "\"}");
      return;
     }
   if(kind == "req")
     {
      HandleRequest(frame);
      return;
     }
   // Unknown types are ignored on purpose: a newer ATLAS build must be able to talk to an
   // older bridge without the bridge treating it as fatal.
  }

//+------------------------------------------------------------------+
//| Commands                                                          |
//+------------------------------------------------------------------+
void HandleRequest(const string frame)
  {
   string id  = JsonGetString(frame, "id", "");
   string op  = JsonGetString(frame, "op", "");
   string args_raw;
   if(!JsonFind(frame, "args", args_raw))
      args_raw = "{}";

   string data = "";
   string error_code = "";
   string error_msg  = "";
   bool ok = true;

   if(op == "account")
      data = AccountJson();
   else if(op == "specs")
      ok = OpSpecs(args_raw, data, error_code, error_msg);
   else if(op == "quote")
      ok = OpQuote(args_raw, data, error_code, error_msg);
   else if(op == "positions")
      data = AtlasPositionsJson(InpMagic);
   else if(op == "orders")
      data = AtlasOrdersJson(InpMagic);
   else if(op == "bars")
      ok = OpBars(args_raw, data, error_code, error_msg);
   else if(op == "order_send")
      ok = AtlasOrderSend(g_trade, args_raw, InpMagic, data, error_code, error_msg);
   else if(op == "position_modify")
      ok = AtlasPositionModify(g_trade, args_raw, InpMagic, data, error_code, error_msg);
   else if(op == "position_close")
      ok = AtlasPositionClose(g_trade, args_raw, InpMagic, data, error_code, error_msg);
   else if(op == "order_cancel")
      ok = OpCancel(args_raw, data, error_code, error_msg);
   else if(op == "find_by_comment")
     {
      string found = AtlasFindByComment(JsonGetString(args_raw, "comment", ""), InpMagic);
      data = (found == "") ? "null" : found;
     }
   else if(op == "history_deals")
      ok = OpHistoryDeals(args_raw, data, error_code, error_msg);
   else
     {
      ok = false;
      error_code = "UNKNOWN_OP";
      error_msg  = "this bridge does not implement '" + op + "'";
     }

   if(ok)
      SendFrame("{\"v\":1,\"t\":\"reply\",\"ts\":" + IntegerToString(AtlasUtcNowMs()) +
                ",\"id\":\"" + JsonEscape(id) + "\",\"ok\":true,\"data\":" +
                (data == "" ? "null" : data) + "}");
   else
      SendFrame("{\"v\":1,\"t\":\"reply\",\"ts\":" + IntegerToString(AtlasUtcNowMs()) +
                ",\"id\":\"" + JsonEscape(id) + "\",\"ok\":false,\"error\":{" +
                JsonStr("code", error_code) + "," + JsonStr("message", error_msg) + "," +
                JsonInt("retcode", (int)g_trade.ResultRetcode()) + "}}");
  }

bool OpSpecs(const string args, string &data, string &error_code, string &error_msg)
  {
   string raw;
   string wanted[];
   if(JsonFind(args, "symbols", raw))
      JsonStringArray(raw, wanted);
   if(ArraySize(wanted) == 0)
      ArrayCopy(wanted, g_symbols);

   string out = "{";
   int written = 0;
   for(int i = 0; i < ArraySize(wanted); i++)
     {
      string spec;
      if(!AtlasSpecJson(wanted[i], spec))
        {
         PrintFormat("AtlasBridge: symbol '%s' is not available at this broker", wanted[i]);
         continue;
        }
      if(written > 0)
         out += ",";
      out += "\"" + JsonEscape(wanted[i]) + "\":" + spec;
      written++;
     }
   data = out + "}";
   if(written == 0)
     {
      error_code = "UNKNOWN_SYMBOL";
      error_msg  = "none of the requested symbols exist at this broker; check for suffixes "
                   "such as .m, _i or .pro";
      return false;
     }
   return true;
  }

bool OpQuote(const string args, string &data, string &error_code, string &error_msg)
  {
   string symbol = JsonGetString(args, "sym", "");
   MqlTick tick;
   if(symbol == "" || !SymbolInfoTick(symbol, tick))
     {
      error_code = "NO_QUOTES";
      error_msg  = "no tick available for '" + symbol + "'";
      return false;
     }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   data = "{" + JsonNum("bid", tick.bid, digits) + "," + JsonNum("ask", tick.ask, digits) +
          "," + JsonInt("ts", AtlasServerToUtcMs(tick.time)) + "}";
   return true;
  }

bool OpBars(const string args, string &data, string &error_code, string &error_msg)
  {
   string symbol = JsonGetString(args, "sym", "");
   string tf_name = JsonGetString(args, "tf", "M5");
   int count = (int)JsonGetLongOr(args, "count", 500);
   count = (int)MathMax(1, MathMin(count, 5000));

   ENUM_TIMEFRAMES tf = TimeframeFromName(tf_name);
   if(tf == PERIOD_CURRENT || symbol == "")
     {
      error_code = "INVALID_ARGS";
      error_msg  = "unknown symbol or timeframe";
      return false;
     }
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   // Start at shift 1: shift 0 is the bar still forming, and sending it as history is the
   // classic multi-timeframe look-ahead bug.
   int got = CopyRates(symbol, tf, 1, count, rates);
   if(got <= 0)
     {
      error_code = "NO_HISTORY";
      error_msg  = StringFormat("CopyRates returned %d for %s %s (error %d); the terminal "
                                "may still be downloading history",
                                got, symbol, tf_name, GetLastError());
      return false;
     }
   string out = "[";
   for(int i = 0; i < got; i++)
     {
      if(i > 0)
         out += ",";
      out += StringFormat("[%I64d,%s,%s,%s,%s,%I64d,%d]",
                          AtlasServerToUtcMs(rates[i].time),
                          DoubleToString(rates[i].open, 8),
                          DoubleToString(rates[i].high, 8),
                          DoubleToString(rates[i].low, 8),
                          DoubleToString(rates[i].close, 8),
                          rates[i].tick_volume, (int)rates[i].spread);
     }
   data = out + "]";
   return true;
  }

bool OpCancel(const string args, string &data, string &error_code, string &error_msg)
  {
   long ticket = JsonGetLongOr(args, "ticket", 0);
   if(ticket <= 0 || !OrderSelect((ulong)ticket))
     {
      error_code = "ORDER_NOT_FOUND";
      error_msg  = StringFormat("no pending order with ticket %I64d", ticket);
      return false;
     }
   if(OrderGetInteger(ORDER_MAGIC) != InpMagic)
     {
      error_code = "NOT_OURS";
      error_msg  = "the order belongs to a different magic number";
      return false;
     }
   g_trade.OrderDelete((ulong)ticket);
   data = "{" + JsonInt("retcode", (int)g_trade.ResultRetcode()) + "," +
          JsonStr("retcode_text", g_trade.ResultRetcodeDescription()) + "}";
   return true;
  }

//+------------------------------------------------------------------+
//| Closed round trips from the deal history.                         |
//|                                                                   |
//| Pairing happens here rather than in Python because MT5 exposes    |
//| DEAL_POSITION_ID on the deal, and re-deriving that association    |
//| from a partial view on the other side would be guesswork.         |
//+------------------------------------------------------------------+
bool OpHistoryDeals(const string args, string &data, string &error_code, string &error_msg)
  {
   long from_ms = JsonGetLongOr(args, "from_ms", 0);
   long offset  = AtlasServerOffsetSeconds();
   datetime from = (datetime)(from_ms / 1000 + offset);
   datetime to   = TimeCurrent() + 86400;
   if(from <= 0)
      from = TimeCurrent() - 30 * 86400;

   if(!HistorySelect(from, to))
     {
      error_code = "HISTORY_FAILED";
      error_msg  = StringFormat("HistorySelect failed, error %d", GetLastError());
      return false;
     }

   string out = "[";
   int written = 0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0)
         continue;
      if(HistoryDealGetInteger(deal, DEAL_MAGIC) != InpMagic)
         continue;
      if(HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
         continue;   // only closing deals complete a round trip

      ulong position_id = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      string symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

      // Find the opening deal of the same position to recover entry price and time.
      double entry_price = 0.0;
      long   entry_time  = 0;
      string comment     = "";
      double commission  = HistoryDealGetDouble(deal, DEAL_COMMISSION);
      for(int j = 0; j < total; j++)
        {
         ulong d2 = HistoryDealGetTicket(j);
         if(d2 == 0)
            continue;
         if((ulong)HistoryDealGetInteger(d2, DEAL_POSITION_ID) != position_id)
            continue;
         if(HistoryDealGetInteger(d2, DEAL_ENTRY) == DEAL_ENTRY_IN)
           {
            entry_price = HistoryDealGetDouble(d2, DEAL_PRICE);
            entry_time  = AtlasServerToUtcMs((datetime)HistoryDealGetInteger(d2, DEAL_TIME));
            comment     = HistoryDealGetString(d2, DEAL_COMMENT);
            commission += HistoryDealGetDouble(d2, DEAL_COMMISSION);
           }
        }
      if(entry_time == 0)
         continue;   // the opening deal is outside the requested window

      bool was_buy = (HistoryDealGetInteger(deal, DEAL_TYPE) == DEAL_TYPE_SELL);

      string parts[];
      ArrayResize(parts, 14);
      int k = 0;
      parts[k++] = JsonInt("trade_id", (long)position_id);
      parts[k++] = JsonInt("position", (long)position_id);
      parts[k++] = JsonBool("closed", true);
      parts[k++] = JsonStr("sym", symbol);
      parts[k++] = JsonStr("side", was_buy ? "BUY" : "SELL");
      parts[k++] = JsonNum("volume", HistoryDealGetDouble(deal, DEAL_VOLUME), 4);
      parts[k++] = JsonNum("entry_price", entry_price, digits);
      parts[k++] = JsonInt("entry_time", entry_time);
      parts[k++] = JsonNum("exit_price", HistoryDealGetDouble(deal, DEAL_PRICE), digits);
      parts[k++] = JsonInt("exit_time",
                           AtlasServerToUtcMs((datetime)HistoryDealGetInteger(deal, DEAL_TIME)));
      parts[k++] = JsonNum("profit", HistoryDealGetDouble(deal, DEAL_PROFIT), 2);
      parts[k++] = JsonNum("commission", commission, 2);
      parts[k++] = JsonNum("swap", HistoryDealGetDouble(deal, DEAL_SWAP), 2);
      parts[k++] = JsonStr("comment", comment);

      if(written > 0)
         out += ",";
      out += JsonObject(parts, k);
      written++;
     }
   data = out + "]";
   return true;
  }

//+------------------------------------------------------------------+
//| Streaming                                                         |
//+------------------------------------------------------------------+
void PublishQuotes()
  {
   if(g_socket == INVALID_HANDLE || !g_welcomed)
      return;
   long now = AtlasUtcNowMs();
   for(int s = 0; s < ArraySize(g_symbols); s++)
     {
      if(now - g_last_tick_ms[s] < InpTickThrottleMs)
         continue;
      MqlTick tick;
      if(!SymbolInfoTick(g_symbols[s], tick))
         continue;
      int digits = (int)SymbolInfoInteger(g_symbols[s], SYMBOL_DIGITS);
      SendFrame("{\"v\":1,\"t\":\"tick\",\"ts\":" +
                IntegerToString(AtlasServerToUtcMs(tick.time)) + "," +
                JsonStr("sym", g_symbols[s]) + "," +
                JsonNum("bid", tick.bid, digits) + "," +
                JsonNum("ask", tick.ask, digits) + "}");
      g_last_tick_ms[s] = now;
     }
  }

//+------------------------------------------------------------------+
//| Emit a bar only once it has CLOSED.                               |
//|                                                                   |
//| Closure is detected by a change in iTime(sym, tf, 0) -- never by  |
//| a wall clock. A clock-based check would emit bars during a market |
//| gap that contain no data, and would drift against the broker's    |
//| own bar boundaries.                                               |
//+------------------------------------------------------------------+
void PublishClosedBars()
  {
   if(g_socket == INVALID_HANDLE || !g_welcomed)
      return;
   int tf_count = ArraySize(g_timeframes);
   for(int s = 0; s < ArraySize(g_symbols); s++)
      for(int t = 0; t < tf_count; t++)
        {
         int idx = s * tf_count + t;
         datetime current = iTime(g_symbols[s], g_timeframes[t], 0);
         if(current == 0 || current == g_last_bar_time[idx])
            continue;
         datetime previous = g_last_bar_time[idx];
         g_last_bar_time[idx] = current;
         if(previous == 0)
            continue;   // first observation: nothing closed, we simply learned where we are

         MqlRates rates[];
         ArraySetAsSeries(rates, false);
         if(CopyRates(g_symbols[s], g_timeframes[t], 1, 1, rates) != 1)
            continue;
         int digits = (int)SymbolInfoInteger(g_symbols[s], SYMBOL_DIGITS);
         SendFrame("{\"v\":1,\"t\":\"bar\",\"ts\":" + IntegerToString(AtlasUtcNowMs()) + "," +
                   JsonStr("sym", g_symbols[s]) + "," +
                   JsonStr("tf", g_tf_names[t]) + "," +
                   JsonInt("open_time", AtlasServerToUtcMs(rates[0].time)) + "," +
                   JsonNum("o", rates[0].open, digits) + "," +
                   JsonNum("h", rates[0].high, digits) + "," +
                   JsonNum("l", rates[0].low, digits) + "," +
                   JsonNum("c", rates[0].close, digits) + "," +
                   JsonInt("vol", (long)rates[0].tick_volume) + "," +
                   JsonInt("spread", (int)rates[0].spread) + "}");
        }
  }

//+------------------------------------------------------------------+
//| Input parsing                                                     |
//+------------------------------------------------------------------+
bool ParseSymbols(const string csv)
  {
   string pieces[];
   int n = StringSplit(csv, ',', pieces);
   ArrayResize(g_symbols, 0);
   for(int i = 0; i < n; i++)
     {
      string s = pieces[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      if(StringLen(s) == 0)
         continue;
      int k = ArraySize(g_symbols);
      ArrayResize(g_symbols, k + 1);
      g_symbols[k] = s;
     }
   return ArraySize(g_symbols) > 0;
  }

ENUM_TIMEFRAMES TimeframeFromName(const string name)
  {
   if(name == "M1")  return PERIOD_M1;
   if(name == "M5")  return PERIOD_M5;
   if(name == "M15") return PERIOD_M15;
   if(name == "M30") return PERIOD_M30;
   if(name == "H1")  return PERIOD_H1;
   if(name == "H4")  return PERIOD_H4;
   if(name == "D1")  return PERIOD_D1;
   if(name == "W1")  return PERIOD_W1;
   return PERIOD_CURRENT;
  }

bool ParseTimeframes(const string csv)
  {
   string pieces[];
   int n = StringSplit(csv, ',', pieces);
   ArrayResize(g_timeframes, 0);
   ArrayResize(g_tf_names, 0);
   for(int i = 0; i < n; i++)
     {
      string s = pieces[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      ENUM_TIMEFRAMES tf = TimeframeFromName(s);
      if(tf == PERIOD_CURRENT)
        {
         if(StringLen(s) > 0)
            Print("AtlasBridge: ignoring unknown timeframe '", s, "'");
         continue;
        }
      int k = ArraySize(g_timeframes);
      ArrayResize(g_timeframes, k + 1);
      ArrayResize(g_tf_names, k + 1);
      g_timeframes[k] = tf;
      g_tf_names[k] = s;
     }
   return ArraySize(g_timeframes) > 0;
  }
//+------------------------------------------------------------------+
