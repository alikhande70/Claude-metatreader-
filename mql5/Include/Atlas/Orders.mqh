//+------------------------------------------------------------------+
//| Atlas/Orders.mqh                                                  |
//| The order layer: sending, modifying, closing, and reporting.      |
//|                                                                   |
//| Two rules govern everything here.                                 |
//|                                                                   |
//| 1. A `true` return from CTrade means the request LEFT THE         |
//|    TERMINAL, not that it filled as asked. The retcode is the only |
//|    thing that says what happened, and it is always read and       |
//|    always reported upstream verbatim.                             |
//|                                                                   |
//| 2. This EA never retries. Retry policy lives in the ATLAS order   |
//|    router, which can check idempotency by client order id before  |
//|    resending (ADR-013). An EA that retries on its own can double  |
//|    fill, because it cannot see what the engine already knows.     |
//+------------------------------------------------------------------+
#property strict

#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>
#include <Atlas/Json.mqh>
#include <Atlas/Spec.mqh>

//+------------------------------------------------------------------+
//| Validate SL/TP against side and the broker's stops level.         |
//| Rejecting here, with a specific reason, is far more useful than   |
//| letting the server answer 10016 with no context.                  |
//+------------------------------------------------------------------+
bool AtlasValidateStops(const string symbol, const bool is_buy, const double reference,
                        const double sl, const double tp, string &error_out)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int min_points = AtlasStopsLevelPoints(symbol);

   if(sl != 0.0)
     {
      if(is_buy && sl >= reference)
        {
         error_out = StringFormat("stop loss %s is not below the buy reference %s",
                                  DoubleToString(sl, digits), DoubleToString(reference, digits));
         return false;
        }
      if(!is_buy && sl <= reference)
        {
         error_out = StringFormat("stop loss %s is not above the sell reference %s",
                                  DoubleToString(sl, digits), DoubleToString(reference, digits));
         return false;
        }
      int dist = (int)MathRound(MathAbs(reference - sl) / point);
      if(dist < min_points)
        {
         error_out = StringFormat("stop loss is %d points away, inside the %d-point stops level",
                                  dist, min_points);
         return false;
        }
     }
   if(tp != 0.0)
     {
      if(is_buy && tp <= reference)
        {
         error_out = StringFormat("take profit %s is not above the buy reference %s",
                                  DoubleToString(tp, digits), DoubleToString(reference, digits));
         return false;
        }
      if(!is_buy && tp >= reference)
        {
         error_out = StringFormat("take profit %s is not below the sell reference %s",
                                  DoubleToString(tp, digits), DoubleToString(reference, digits));
         return false;
        }
      int dist = (int)MathRound(MathAbs(reference - tp) / point);
      if(dist < min_points)
        {
         error_out = StringFormat("take profit is %d points away, inside the %d-point stops level",
                                  dist, min_points);
         return false;
        }
     }
   return true;
  }

//+------------------------------------------------------------------+
//| Result of an order operation, rendered as protocol JSON.          |
//+------------------------------------------------------------------+
string AtlasTradeResultJson(CTrade &trade, const double requested_price, const int digits)
  {
   string parts[];
   ArrayResize(parts, 8);
   int i = 0;
   parts[i++] = JsonInt("retcode", (int)trade.ResultRetcode());
   parts[i++] = JsonStr("retcode_text", trade.ResultRetcodeDescription());
   parts[i++] = JsonInt("order", (long)trade.ResultOrder());
   parts[i++] = JsonInt("deal", (long)trade.ResultDeal());
   parts[i++] = JsonInt("position", (long)trade.ResultOrder());
   parts[i++] = JsonNum("volume", trade.ResultVolume(), 4);
   parts[i++] = JsonNum("price", trade.ResultPrice(), digits);
   parts[i++] = JsonNum("requested_price", requested_price, digits);
   return JsonObject(parts, i);
  }

//+------------------------------------------------------------------+
//| Send an order described by protocol `order_send` args.            |
//+------------------------------------------------------------------+
bool AtlasOrderSend(CTrade &trade, const string args, const long magic,
                    string &data_out, string &error_code, string &error_msg)
  {
   string symbol = JsonGetString(args, "sym");
   if(symbol == "" || !SymbolSelect(symbol, true))
     {
      error_code = "UNKNOWN_SYMBOL";
      error_msg  = "symbol '" + symbol + "' is not available at this broker; check for a "
                   "suffix such as .m or _i";
      return false;
     }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      error_code = "TRADE_NOT_ALLOWED";
      error_msg  = "algorithmic trading is disabled for this EA (check the Algo Trading "
                   "button and the EA's own 'Allow algo trading' setting)";
      return false;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     {
      error_code = "TRADE_NOT_ALLOWED";
      error_msg  = "algorithmic trading is disabled in the terminal";
      return false;
     }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
     {
      error_code = "TRADE_NOT_ALLOWED";
      error_msg  = "the trade server has disabled expert trading for this account";
      return false;
     }

   string side = JsonGetString(args, "side", "BUY");
   string otype = JsonGetString(args, "type", "MARKET");
   bool is_buy = (side == "BUY");
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   double raw_volume = JsonGetDoubleOr(args, "volume", 0.0);
   double volume = AtlasNormalizeVolume(symbol, raw_volume);
   if(volume <= 0.0)
     {
      error_code = "INVALID_VOLUME";
      error_msg  = StringFormat("volume %s is below the %s minimum for %s",
                                DoubleToString(raw_volume, 4),
                                DoubleToString(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN), 4),
                                symbol);
      return false;
     }

   double sl = 0.0, tp = 0.0;
   if(JsonGetDouble(args, "sl", sl))
      sl = AtlasNormalizePrice(symbol, sl);
   if(JsonGetDouble(args, "tp", tp))
      tp = AtlasNormalizePrice(symbol, tp);

   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   if(ask <= 0.0 || bid <= 0.0)
     {
      error_code = "NO_QUOTES";
      error_msg  = "no current quotes for " + symbol + "; the market may be closed";
      return false;
     }

   double reference = is_buy ? ask : bid;
   double price = 0.0;
   if(otype != "MARKET")
     {
      if(!JsonGetDouble(args, "price", price) || price <= 0.0)
        {
         error_code = "INVALID_PRICE";
         error_msg  = "a pending order requires a price";
         return false;
        }
      price = AtlasNormalizePrice(symbol, price);
      reference = price;
     }

   string stops_error = "";
   if(!AtlasValidateStops(symbol, is_buy, reference, sl, tp, stops_error))
     {
      error_code = "INVALID_STOPS";
      error_msg  = stops_error;
      return false;
     }

   int deviation = (int)JsonGetLongOr(args, "deviation", 20);
   string comment = JsonGetString(args, "comment", "");
   // Many brokers truncate the order comment (commonly at 31 characters) and some overwrite
   // it. ATLAS keeps its client order id to 16 characters for exactly this reason; truncate
   // defensively rather than let the server do it unpredictably.
   if(StringLen(comment) > 31)
      comment = StringSubstr(comment, 0, 31);

   trade.SetExpertMagicNumber(magic);
   trade.SetDeviationInPoints(deviation);
   trade.SetTypeFillingBySymbol(symbol);
   ENUM_ORDER_TYPE_FILLING filling =
      AtlasResolveFilling(symbol, JsonGetString(args, "filling", "IOC"));
   trade.SetTypeFilling(filling);

   bool sent = false;
   if(otype == "MARKET")
      sent = is_buy ? trade.Buy(volume, symbol, 0.0, sl, tp, comment)
                    : trade.Sell(volume, symbol, 0.0, sl, tp, comment);
   else if(otype == "LIMIT")
      sent = is_buy ? trade.BuyLimit(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment)
                    : trade.SellLimit(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
   else if(otype == "STOP")
      sent = is_buy ? trade.BuyStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment)
                    : trade.SellStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
   else
     {
      error_code = "INVALID_ORDER_TYPE";
      error_msg  = "unsupported order type '" + otype + "'";
      return false;
     }

   // `sent` false means the request never reached the server. The retcode still carries the
   // reason, so the result is reported either way rather than being turned into a generic
   // error that the engine cannot act on.
   data_out = AtlasTradeResultJson(trade, reference, digits);
   if(!sent && trade.ResultRetcode() == 0)
     {
      error_code = "SEND_FAILED";
      error_msg  = StringFormat("OrderSend failed locally, GetLastError=%d", GetLastError());
      return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| Modify a position's protective levels.                            |
//| Refuses to touch a position that is not ours (magic mismatch), so |
//| a manual trade in the same terminal is safe from the bridge.      |
//+------------------------------------------------------------------+
bool AtlasPositionModify(CTrade &trade, const string args, const long magic,
                         string &data_out, string &error_code, string &error_msg)
  {
   long ticket = JsonGetLongOr(args, "ticket", 0);
   if(ticket <= 0 || !PositionSelectByTicket((ulong)ticket))
     {
      error_code = "POSITION_NOT_FOUND";
      error_msg  = StringFormat("no open position with ticket %I64d", ticket);
      return false;
     }
   if(PositionGetInteger(POSITION_MAGIC) != magic)
     {
      error_code = "NOT_OURS";
      error_msg  = "the position belongs to a different magic number and will not be modified";
      return false;
     }
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   bool is_buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);

   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double v;
   if(JsonGetDouble(args, "sl", v))
      sl = AtlasNormalizePrice(symbol, v);
   if(JsonGetDouble(args, "tp", v))
      tp = AtlasNormalizePrice(symbol, v);

   double current = is_buy ? SymbolInfoDouble(symbol, SYMBOL_BID)
                           : SymbolInfoDouble(symbol, SYMBOL_ASK);
   // The freeze level is a zone around price where the server refuses modification. Failing
   // fast with a named reason beats a 10029 the engine has to guess at.
   int freeze = AtlasFreezeLevelPoints(symbol);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(freeze > 0 && sl != 0.0 && MathAbs(current - sl) < freeze * point)
     {
      error_code = "FROZEN";
      error_msg  = StringFormat("stop loss is inside the %d-point freeze level", freeze);
      return false;
     }
   string stops_error = "";
   if(!AtlasValidateStops(symbol, is_buy, current, sl, tp, stops_error))
     {
      error_code = "INVALID_STOPS";
      error_msg  = stops_error;
      return false;
     }

   trade.SetExpertMagicNumber(magic);
   trade.PositionModify((ulong)ticket, sl, tp);
   data_out = AtlasTradeResultJson(trade, current, digits);
   return true;
  }

//+------------------------------------------------------------------+
//| Close a position, fully or partially.                             |
//+------------------------------------------------------------------+
bool AtlasPositionClose(CTrade &trade, const string args, const long magic,
                        string &data_out, string &error_code, string &error_msg)
  {
   long ticket = JsonGetLongOr(args, "ticket", 0);
   if(ticket <= 0 || !PositionSelectByTicket((ulong)ticket))
     {
      error_code = "POSITION_NOT_FOUND";
      error_msg  = StringFormat("no open position with ticket %I64d", ticket);
      return false;
     }
   if(PositionGetInteger(POSITION_MAGIC) != magic)
     {
      error_code = "NOT_OURS";
      error_msg  = "the position belongs to a different magic number and will not be closed";
      return false;
     }
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double open_volume = PositionGetDouble(POSITION_VOLUME);

   double want = 0.0;
   bool partial = JsonGetDouble(args, "volume", want) && want > 0.0 && want < open_volume;
   int deviation = (int)JsonGetLongOr(args, "deviation", 50);
   trade.SetExpertMagicNumber(magic);
   trade.SetDeviationInPoints(deviation);
   trade.SetTypeFillingBySymbol(symbol);

   if(partial)
      trade.PositionClosePartial((ulong)ticket, AtlasNormalizeVolume(symbol, want), deviation);
   else
      trade.PositionClose((ulong)ticket, deviation);

   data_out = AtlasTradeResultJson(trade, PositionGetDouble(POSITION_PRICE_CURRENT), digits);
   return true;
  }

//+------------------------------------------------------------------+
//| One open position as protocol JSON.                               |
//+------------------------------------------------------------------+
string AtlasPositionJson(const ulong ticket)
  {
   if(!PositionSelectByTicket(ticket))
      return "";
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   bool is_buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);

   string parts[];
   ArrayResize(parts, 14);
   int i = 0;
   parts[i++] = JsonInt("ticket", (long)ticket);
   parts[i++] = JsonStr("sym", symbol);
   parts[i++] = JsonStr("side", is_buy ? "BUY" : "SELL");
   parts[i++] = JsonNum("volume", PositionGetDouble(POSITION_VOLUME), 4);
   parts[i++] = JsonNum("open_price", PositionGetDouble(POSITION_PRICE_OPEN), digits);
   parts[i++] = JsonInt("open_time",
                        AtlasServerToUtcMs((datetime)PositionGetInteger(POSITION_TIME)));
   parts[i++] = JsonPrice("sl", PositionGetDouble(POSITION_SL), digits);
   parts[i++] = JsonPrice("tp", PositionGetDouble(POSITION_TP), digits);
   parts[i++] = JsonNum("price_current", PositionGetDouble(POSITION_PRICE_CURRENT), digits);
   parts[i++] = JsonNum("profit", PositionGetDouble(POSITION_PROFIT), 2);
   parts[i++] = JsonNum("swap", PositionGetDouble(POSITION_SWAP), 2);
   parts[i++] = JsonNum("commission", 0.0, 2);  // MT5 books commission on the deal, not here
   parts[i++] = JsonInt("magic", PositionGetInteger(POSITION_MAGIC));
   parts[i++] = JsonStr("comment", PositionGetString(POSITION_COMMENT));
   return JsonObject(parts, i);
  }

//+------------------------------------------------------------------+
//| Every position carrying our magic, as a JSON array.               |
//+------------------------------------------------------------------+
string AtlasPositionsJson(const long magic)
  {
   string out = "[";
   int written = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      string one = AtlasPositionJson(ticket);
      if(one == "")
         continue;
      if(written > 0)
         out += ",";
      out += one;
      written++;
     }
   return out + "]";
  }

//+------------------------------------------------------------------+
//| Find a position by its order comment -- the idempotency lookup.   |
//|                                                                   |
//| This is what makes retry safe (ADR-013). It must answer           |
//| "definitely not present" or "here it is", never "I could not      |
//| check": an unreliable answer here produces double fills.          |
//+------------------------------------------------------------------+
string AtlasFindByComment(const string comment, const long magic)
  {
   if(comment == "")
      return "";
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      string c = PositionGetString(POSITION_COMMENT);
      if(StringFind(c, comment, 0) >= 0)
         return AtlasPositionJson(ticket);
     }
   return "";
  }

//+------------------------------------------------------------------+
//| Pending orders carrying our magic.                                |
//+------------------------------------------------------------------+
string AtlasOrdersJson(const long magic)
  {
   string out = "[";
   int written = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) != magic)
         continue;
      string symbol = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      bool is_buy = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP);
      string kind = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_SELL_LIMIT)
                    ? "LIMIT" : "STOP";

      string parts[];
      ArrayResize(parts, 10);
      int k = 0;
      parts[k++] = JsonInt("ticket", (long)ticket);
      parts[k++] = JsonStr("sym", symbol);
      parts[k++] = JsonStr("side", is_buy ? "BUY" : "SELL");
      parts[k++] = JsonStr("type", kind);
      parts[k++] = JsonNum("volume", OrderGetDouble(ORDER_VOLUME_CURRENT), 4);
      parts[k++] = JsonNum("price", OrderGetDouble(ORDER_PRICE_OPEN), digits);
      parts[k++] = JsonPrice("sl", OrderGetDouble(ORDER_SL), digits);
      parts[k++] = JsonPrice("tp", OrderGetDouble(ORDER_TP), digits);
      parts[k++] = JsonInt("setup_time",
                           AtlasServerToUtcMs((datetime)OrderGetInteger(ORDER_TIME_SETUP)));
      parts[k++] = JsonInt("magic", OrderGetInteger(ORDER_MAGIC));

      if(written > 0)
         out += ",";
      out += JsonObject(parts, k);
      written++;
     }
   return out + "]";
  }
//+------------------------------------------------------------------+
