//+------------------------------------------------------------------+
//| Atlas/Spec.mqh                                                    |
//| Symbol specification export and the broker-compatibility helpers  |
//| that every order must pass through.                               |
//|                                                                   |
//| Nothing here is hardcoded. Gold quotes at 2 digits on some        |
//| brokers and 3 on others; contract size can be 100 oz or 10;       |
//| symbol names carry suffixes (.m, _i, .pro); filling modes differ  |
//| per symbol. Every one of those has to be READ, and a wrong value  |
//| does not throw -- it silently changes every position size.        |
//+------------------------------------------------------------------+
#property strict

#include <Atlas/Json.mqh>

//+------------------------------------------------------------------+
//| Filling modes as a JSON array, read from the symbol's bit mask.   |
//| Hardcoding FOK is a guaranteed retcode 10030 on brokers that only |
//| allow IOC, and it is one of the most common reasons an EA that    |
//| works on one broker never fills on another.                       |
//+------------------------------------------------------------------+
string AtlasFillingModesJson(const string symbol)
  {
   long mask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   string parts = "";
   if((mask & SYMBOL_FILLING_FOK) != 0)
      parts += "\"FOK\"";
   if((mask & SYMBOL_FILLING_IOC) != 0)
      parts += (StringLen(parts) > 0 ? ",\"IOC\"" : "\"IOC\"");
   // RETURN is always available for pending orders; report it so the engine knows the
   // fallback exists rather than assuming it.
   parts += (StringLen(parts) > 0 ? ",\"RETURN\"" : "\"RETURN\"");
   return "[" + parts + "]";
  }

//+------------------------------------------------------------------+
//| Translate a protocol filling name into the enum.                  |
//| Falls back to a mode the SYMBOL actually supports rather than to  |
//| a constant, because the fallback is what runs when the engine and |
//| the broker disagree.                                              |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING AtlasResolveFilling(const string symbol, const string requested)
  {
   long mask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if(requested == "FOK" && (mask & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if(requested == "IOC" && (mask & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   if(requested == "RETURN")
      return ORDER_FILLING_RETURN;
   if((mask & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   if((mask & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
  }

//+------------------------------------------------------------------+
//| Round a volume DOWN to the broker's lot grid and clamp it.        |
//| Down, never to nearest: rounding up silently exceeds the risk     |
//| budget that produced this number.                                 |
//+------------------------------------------------------------------+
double AtlasNormalizeVolume(const string symbol, const double volume)
  {
   double vmin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = 0.01;
   double steps = MathFloor(volume / vstep + 1e-9);
   double out = steps * vstep;
   if(out < vmin)
      return 0.0;
   if(out > vmax)
      out = vmax;
   // Re-round to the step's own precision to remove binary float residue that the server
   // would otherwise reject as retcode 10014.
   int step_digits = 0;
   double probe = vstep;
   while(probe < 1.0 && step_digits < 8)
     {
      probe *= 10.0;
      step_digits++;
     }
   return NormalizeDouble(out, step_digits);
  }

double AtlasNormalizePrice(const string symbol, const double price)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick > 0.0)
      return NormalizeDouble(MathRound(price / tick) * tick, digits);
   return NormalizeDouble(price, digits);
  }

//+------------------------------------------------------------------+
//| Minimum SL/TP distance in points.                                 |
//|                                                                   |
//| When the broker reports 0 it is using a DYNAMIC level tied to the |
//| current spread, not "no restriction". Returning 0 there is how an |
//| EA ends up placing stops that are silently rejected, so a         |
//| spread-based floor is applied instead.                            |
//+------------------------------------------------------------------+
int AtlasStopsLevelPoints(const string symbol)
  {
   int level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(level > 0)
      return level;
   int spread = (int)SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   return (int)MathMax(spread * 2, 10);
  }

int AtlasFreezeLevelPoints(const string symbol)
  {
   return (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL);
  }

//+------------------------------------------------------------------+
//| Full specification as a JSON object.                              |
//| This is what the engine sizes every position from, so a symbol    |
//| that cannot be selected is reported as an error rather than       |
//| filled in with plausible defaults.                                |
//+------------------------------------------------------------------+
bool AtlasSpecJson(const string symbol, string &out)
  {
   if(!SymbolSelect(symbol, true))
     {
      out = "";
      return false;
     }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tick_size  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double point      = SymbolInfoDouble(symbol, SYMBOL_POINT);

   string parts[];
   ArrayResize(parts, 24);
   int i = 0;
   parts[i++] = JsonStr("description", SymbolInfoString(symbol, SYMBOL_DESCRIPTION));
   parts[i++] = JsonInt("digits", digits);
   parts[i++] = JsonNum("point", point, 10);
   parts[i++] = JsonNum("tick_size", tick_size, 10);
   parts[i++] = JsonNum("tick_value", tick_value, 10);
   parts[i++] = JsonNum("contract_size", SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE), 4);
   parts[i++] = JsonNum("volume_min", SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN), 4);
   parts[i++] = JsonNum("volume_max", SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX), 4);
   parts[i++] = JsonNum("volume_step", SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP), 4);
   parts[i++] = JsonInt("stops_level", AtlasStopsLevelPoints(symbol));
   parts[i++] = JsonInt("freeze_level", AtlasFreezeLevelPoints(symbol));
   parts[i++] = JsonStr("currency_base", SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE));
   parts[i++] = JsonStr("currency_profit", SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT));
   parts[i++] = JsonStr("currency_margin", SymbolInfoString(symbol, SYMBOL_CURRENCY_MARGIN));
   parts[i++] = JsonNum("margin_initial", SymbolInfoDouble(symbol, SYMBOL_MARGIN_INITIAL), 4);
   parts[i++] = JsonNum("swap_long", SymbolInfoDouble(symbol, SYMBOL_SWAP_LONG), 6);
   parts[i++] = JsonNum("swap_short", SymbolInfoDouble(symbol, SYMBOL_SWAP_SHORT), 6);
   parts[i++] = JsonInt("swap_mode", (int)SymbolInfoInteger(symbol, SYMBOL_SWAP_MODE));
   // The triple-swap weekday varies by BROKER and by SYMBOL. Report the broker's own value
   // rather than assuming Wednesday. MT5 uses 0=Sunday; ATLAS uses 0=Monday.
   int rollover3 = (int)SymbolInfoInteger(symbol, SYMBOL_SWAP_ROLLOVER3DAYS);
   parts[i++] = JsonInt("swap_rollover_3days", (rollover3 + 6) % 7);
   parts[i++] = JsonBool("trade_allowed",
                         SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_DISABLED);
   parts[i++] = "\"filling_modes\":" + AtlasFillingModesJson(symbol);
   out = JsonObject(parts, i);
   return true;
  }

//+------------------------------------------------------------------+
//| Broker server time offset from UTC, in seconds.                   |
//| Measured every time it is asked for, never cached across a DST    |
//| boundary, because the offset moves twice a year and the shift     |
//| lands exactly on session opens.                                   |
//+------------------------------------------------------------------+
long AtlasServerOffsetSeconds()
  {
   return (long)(TimeCurrent() - TimeGMT());
  }

//| Convert a broker server timestamp to epoch milliseconds UTC.
long AtlasServerToUtcMs(const datetime server_time)
  {
   return ((long)server_time - AtlasServerOffsetSeconds()) * 1000;
  }

long AtlasUtcNowMs()
  {
   return (long)TimeGMT() * 1000;
  }
//+------------------------------------------------------------------+
