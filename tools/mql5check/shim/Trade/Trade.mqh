//===================================================================
// Shim for <Trade/Trade.mqh>. Signatures transcribed from the MQL5
// standard library so that call sites in the bridge are checked for
// argument count, order and type. Bodies are absent on purpose: this
// header exists to be compiled against, not run.
//===================================================================
#pragma once
#include "../mql5.hpp"

class CTrade
  {
public:
   CTrade();

   void   LogLevel(const uint log_level);
   void   SetExpertMagicNumber(const ulong magic);
   void   SetDeviationInPoints(const ulong deviation);
   void   SetTypeFilling(const ENUM_ORDER_TYPE_FILLING filling);
   bool   SetTypeFillingBySymbol(const string symbol);
   void   SetAsyncMode(const bool mode);

   bool   Buy(const double volume, const string symbol = string(), double price = 0.0,
              const double sl = 0.0, const double tp = 0.0, const string comment = string());
   bool   Sell(const double volume, const string symbol = string(), double price = 0.0,
               const double sl = 0.0, const double tp = 0.0, const string comment = string());
   bool   BuyLimit(const double volume, const double price, const string symbol = string(),
                   const double sl = 0.0, const double tp = 0.0,
                   const ENUM_ORDER_TYPE_TIME type_time = ORDER_TIME_GTC,
                   const datetime expiration = 0, const string comment = string());
   bool   SellLimit(const double volume, const double price, const string symbol = string(),
                    const double sl = 0.0, const double tp = 0.0,
                    const ENUM_ORDER_TYPE_TIME type_time = ORDER_TIME_GTC,
                    const datetime expiration = 0, const string comment = string());
   bool   BuyStop(const double volume, const double price, const string symbol = string(),
                  const double sl = 0.0, const double tp = 0.0,
                  const ENUM_ORDER_TYPE_TIME type_time = ORDER_TIME_GTC,
                  const datetime expiration = 0, const string comment = string());
   bool   SellStop(const double volume, const double price, const string symbol = string(),
                   const double sl = 0.0, const double tp = 0.0,
                   const ENUM_ORDER_TYPE_TIME type_time = ORDER_TIME_GTC,
                   const datetime expiration = 0, const string comment = string());

   bool   PositionModify(const ulong ticket, const double sl, const double tp);
   bool   PositionClose(const ulong ticket, const ulong deviation = ULONG_MAX_MQL);
   bool   PositionClosePartial(const ulong ticket, const double volume,
                               const ulong deviation = ULONG_MAX_MQL);
   bool   OrderDelete(const ulong ticket);
   bool   OrderSend(const MqlTradeRequest &request, MqlTradeResult &result);

   uint   ResultRetcode() const;
   string ResultRetcodeDescription() const;
   ulong  ResultDeal() const;
   ulong  ResultOrder() const;
   double ResultVolume() const;
   double ResultPrice() const;
   double ResultBid() const;
   double ResultAsk() const;
   string ResultComment() const;
  };
