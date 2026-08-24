#pragma once
#include "../mql5.hpp"

class CPositionInfo
  {
public:
   CPositionInfo();
   bool     SelectByTicket(const ulong ticket);
   bool     SelectByIndex(const int index);
   ulong    Ticket() const;
   ulong    Magic() const;
   string   Symbol() const;
   double   Volume() const;
   double   PriceOpen() const;
   double   StopLoss() const;
   double   TakeProfit() const;
   ENUM_POSITION_TYPE PositionType() const;
  };
