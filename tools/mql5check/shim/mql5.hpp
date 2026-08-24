//===================================================================
// mql5.hpp -- a C++ stand-in for the MQL5 builtin surface.
//
// WHAT THIS IS FOR
//   MetaEditor is not available in every environment ATLAS is worked
//   on in, and "it will probably compile" is not evidence. This
//   header, plus tools/mql5check/mql5check.py, lets a normal C++
//   compiler type-check the bridge sources: undeclared identifiers,
//   misspelled API names, wrong argument counts, wrong argument
//   types and wrong property-enum families all become hard errors.
//
// WHAT THIS IS NOT
//   It is NOT a MetaEditor compile and must never be reported as
//   one. MQL5 and C++ differ in ways this cannot model -- object
//   pointer semantics, its own template rules, `#property` handling,
//   implicit numeric-to-string conversion in `+`, and the exact
//   contents of the standard library headers. A clean run here means
//   "no error of the kind a C++ compiler can see", nothing more.
//
// Signatures below are transcribed from the MQL5 reference. Where a
// builtin is overloaded on numeric type, a template is used instead
// so the check does not invent errors MQL5 would not raise.
//===================================================================
#pragma once

#include <cstdint>
#include <cstdio>
#include <string>
#include <type_traits>
#include <vector>

//-------------------------------------------------------------------
// Scalar types
//-------------------------------------------------------------------
// MQL5's `long` is 64-bit, as it is on any LP64 host, so plain `long`
// is left alone rather than macro-redefined -- redefining it would
// corrupt the standard headers this shim itself depends on.
typedef unsigned char      uchar;
typedef unsigned short     ushort;
typedef unsigned int       uint;
typedef unsigned long      ulong;   // 64-bit on LP64, and identical to glibc's own
                                    // typedef, which would otherwise conflict
typedef long long          datetime;
typedef unsigned int       color;
static_assert(sizeof(long) == 8, "MQL5 long is 64-bit; this shim assumes an LP64 host");

//-------------------------------------------------------------------
// string
//
// A distinct type rather than std::string so that MQL5's own
// conversion rules can be modelled: concatenation with a number is
// legal in MQL5 and must not be reported as an error here.
//-------------------------------------------------------------------
class string
  {
public:
   std::string s;
   string() {}
   string(const char *p) : s(p ? p : "") {}
   string(const std::string &v) : s(v) {}
   const char *c_str() const { return s.c_str(); }
   string &operator+=(const string &o) { s += o.s; return *this; }
   template <class T,
             class = typename std::enable_if<std::is_arithmetic<T>::value>::type>
   string &operator+=(T v) { s += std::to_string(v); return *this; }
  };

inline string operator+(const string &a, const string &b) { return string(a.s + b.s); }
template <class T, class = typename std::enable_if<std::is_arithmetic<T>::value>::type>
inline string operator+(const string &a, T v) { return string(a.s + std::to_string(v)); }
template <class T, class = typename std::enable_if<std::is_arithmetic<T>::value>::type>
inline string operator+(T v, const string &a) { return string(std::to_string(v) + a.s); }
inline bool operator==(const string &a, const string &b) { return a.s == b.s; }
inline bool operator!=(const string &a, const string &b) { return a.s != b.s; }
inline bool operator<(const string &a, const string &b) { return a.s < b.s; }

#define NULL_STRING string()

//-------------------------------------------------------------------
// Dynamic arrays
//
// `T name[]` in MQL5 is a resizable array, which has no C++ spelling;
// the preprocessor rewrites those declarations to MqlArray<T>.
//-------------------------------------------------------------------
template <class T>
class MqlArray
  {
public:
   std::vector<T> v;
   T &operator[](int i) { return v[(size_t)i]; }
   const T &operator[](int i) const { return v[(size_t)i]; }
   T &operator[](ulong i) { return v[(size_t)i]; }
   const T &operator[](ulong i) const { return v[(size_t)i]; }
  };

template <class T> inline int ArraySize(const MqlArray<T> &a) { return (int)a.v.size(); }
template <class T> inline int ArrayResize(MqlArray<T> &a, int n, int reserve = 0)
  { (void)reserve; a.v.resize((size_t)(n < 0 ? 0 : n)); return n; }
template <class T, class V> inline void ArrayInitialize(MqlArray<T> &a, V value)
  { for(size_t i = 0; i < a.v.size(); i++) a.v[i] = (T)value; }
template <class T> inline bool ArraySetAsSeries(MqlArray<T> &a, bool flag)
  { (void)a; (void)flag; return true; }
template <class T> inline int ArrayCopy(MqlArray<T> &dst, const MqlArray<T> &src,
                                        int dst_start = 0, int src_start = 0, int count = 0)
  { (void)dst_start; (void)src_start; (void)count; dst.v = src.v; return (int)src.v.size(); }
template <class T> inline bool ArrayFree(MqlArray<T> &a) { a.v.clear(); return true; }

#define WHOLE_ARRAY (-1)

//-------------------------------------------------------------------
// Structures
//-------------------------------------------------------------------
struct MqlTick
  {
   datetime time;   double bid;   double ask;   double last;
   ulong    volume; long     time_msc; uint  flags; double volume_real;
   MqlTick() : time(0), bid(0), ask(0), last(0), volume(0), time_msc(0), flags(0), volume_real(0) {}
  };

struct MqlRates
  {
   datetime time;  double open; double high; double low; double close;
   long tick_volume; int spread; long real_volume;
   MqlRates() : time(0), open(0), high(0), low(0), close(0), tick_volume(0), spread(0), real_volume(0) {}
  };

//-------------------------------------------------------------------
// Enumerations -- kept as distinct types so that passing a symbol
// property where an account property belongs is a compile error, the
// way it is in MetaEditor.
//-------------------------------------------------------------------
enum ENUM_TIMEFRAMES
  {
   PERIOD_CURRENT = 0, PERIOD_M1 = 1, PERIOD_M2, PERIOD_M3, PERIOD_M4, PERIOD_M5,
   PERIOD_M6, PERIOD_M10, PERIOD_M12, PERIOD_M15, PERIOD_M20, PERIOD_M30,
   PERIOD_H1, PERIOD_H2, PERIOD_H3, PERIOD_H4, PERIOD_H6, PERIOD_H8, PERIOD_H12,
   PERIOD_D1, PERIOD_W1, PERIOD_MN1
  };

enum ENUM_SYMBOL_INFO_INTEGER
  {
   SYMBOL_SELECT, SYMBOL_VISIBLE, SYMBOL_SESSION_DEALS, SYMBOL_DIGITS, SYMBOL_SPREAD,
   SYMBOL_SPREAD_FLOAT, SYMBOL_TICKS_BOOKDEPTH, SYMBOL_TRADE_CALC_MODE, SYMBOL_TRADE_MODE,
   SYMBOL_START_TIME, SYMBOL_EXPIRATION_TIME, SYMBOL_TRADE_STOPS_LEVEL,
   SYMBOL_TRADE_FREEZE_LEVEL, SYMBOL_TRADE_EXEMODE, SYMBOL_SWAP_MODE,
   SYMBOL_SWAP_ROLLOVER3DAYS, SYMBOL_MARGIN_HEDGED_USE_LEG, SYMBOL_EXPIRATION_MODE,
   SYMBOL_FILLING_MODE, SYMBOL_ORDER_MODE, SYMBOL_ORDER_GTC_MODE, SYMBOL_OPTION_MODE,
   SYMBOL_OPTION_RIGHT, SYMBOL_TIME, SYMBOL_TIME_MSC, SYMBOL_CUSTOM, SYMBOL_BACKGROUND_COLOR,
   SYMBOL_CHART_MODE, SYMBOL_EXIST, SYMBOL_VOLUME, SYMBOL_VOLUMEHIGH, SYMBOL_VOLUMELOW
  };

enum ENUM_SYMBOL_INFO_DOUBLE
  {
   SYMBOL_BID, SYMBOL_BIDHIGH, SYMBOL_BIDLOW, SYMBOL_ASK, SYMBOL_ASKHIGH, SYMBOL_ASKLOW,
   SYMBOL_LAST, SYMBOL_POINT, SYMBOL_TRADE_TICK_VALUE, SYMBOL_TRADE_TICK_VALUE_PROFIT,
   SYMBOL_TRADE_TICK_VALUE_LOSS, SYMBOL_TRADE_TICK_SIZE, SYMBOL_TRADE_CONTRACT_SIZE,
   SYMBOL_VOLUME_MIN, SYMBOL_VOLUME_MAX, SYMBOL_VOLUME_STEP, SYMBOL_VOLUME_LIMIT,
   SYMBOL_SWAP_LONG, SYMBOL_SWAP_SHORT, SYMBOL_MARGIN_INITIAL, SYMBOL_MARGIN_MAINTENANCE,
   SYMBOL_SESSION_VOLUME, SYMBOL_SESSION_TURNOVER, SYMBOL_TRADE_ACCRUED_INTEREST,
   SYMBOL_TRADE_FACE_VALUE, SYMBOL_TRADE_LIQUIDITY_RATE
  };

enum ENUM_SYMBOL_INFO_STRING
  {
   SYMBOL_CURRENCY_BASE, SYMBOL_CURRENCY_PROFIT, SYMBOL_CURRENCY_MARGIN, SYMBOL_DESCRIPTION,
   SYMBOL_PATH, SYMBOL_BANK, SYMBOL_ISIN, SYMBOL_FORMULA, SYMBOL_PAGE, SYMBOL_CATEGORY,
   SYMBOL_EXCHANGE
  };

enum ENUM_SYMBOL_TRADE_MODE
  { SYMBOL_TRADE_MODE_DISABLED = 0, SYMBOL_TRADE_MODE_LONGONLY, SYMBOL_TRADE_MODE_SHORTONLY,
    SYMBOL_TRADE_MODE_CLOSEONLY, SYMBOL_TRADE_MODE_FULL };

// Filling-mode bit flags on SYMBOL_FILLING_MODE.
#define SYMBOL_FILLING_FOK 1
#define SYMBOL_FILLING_IOC 2
#define SYMBOL_FILLING_BOC 4

enum ENUM_ACCOUNT_INFO_INTEGER
  { ACCOUNT_LOGIN, ACCOUNT_TRADE_MODE, ACCOUNT_LEVERAGE, ACCOUNT_LIMIT_ORDERS,
    ACCOUNT_MARGIN_SO_MODE, ACCOUNT_TRADE_ALLOWED, ACCOUNT_TRADE_EXPERT,
    ACCOUNT_MARGIN_MODE, ACCOUNT_CURRENCY_DIGITS, ACCOUNT_FIFO_CLOSE };

enum ENUM_ACCOUNT_INFO_DOUBLE
  { ACCOUNT_BALANCE, ACCOUNT_CREDIT, ACCOUNT_PROFIT, ACCOUNT_EQUITY, ACCOUNT_MARGIN,
    ACCOUNT_MARGIN_FREE, ACCOUNT_MARGIN_LEVEL, ACCOUNT_MARGIN_SO_CALL,
    ACCOUNT_MARGIN_SO_SO, ACCOUNT_MARGIN_INITIAL, ACCOUNT_MARGIN_MAINTENANCE,
    ACCOUNT_ASSETS, ACCOUNT_LIABILITIES, ACCOUNT_COMMISSION_BLOCKED };

enum ENUM_ACCOUNT_INFO_STRING
  { ACCOUNT_NAME, ACCOUNT_SERVER, ACCOUNT_CURRENCY, ACCOUNT_COMPANY };

enum ENUM_ACCOUNT_MARGIN_MODE
  { ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0, ACCOUNT_MARGIN_MODE_EXCHANGE,
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING };

enum ENUM_POSITION_PROPERTY_INTEGER
  { POSITION_TICKET, POSITION_TIME, POSITION_TIME_MSC, POSITION_TIME_UPDATE,
    POSITION_TIME_UPDATE_MSC, POSITION_TYPE, POSITION_MAGIC, POSITION_IDENTIFIER,
    POSITION_REASON };

enum ENUM_POSITION_PROPERTY_DOUBLE
  { POSITION_VOLUME, POSITION_PRICE_OPEN, POSITION_SL, POSITION_TP, POSITION_PRICE_CURRENT,
    POSITION_SWAP, POSITION_PROFIT };

enum ENUM_POSITION_PROPERTY_STRING
  { POSITION_SYMBOL, POSITION_COMMENT, POSITION_EXTERNAL_ID };

enum ENUM_POSITION_TYPE { POSITION_TYPE_BUY = 0, POSITION_TYPE_SELL = 1 };

enum ENUM_ORDER_PROPERTY_INTEGER
  { ORDER_TICKET, ORDER_TIME_SETUP, ORDER_TYPE, ORDER_STATE, ORDER_TIME_EXPIRATION,
    ORDER_TIME_DONE, ORDER_TIME_SETUP_MSC, ORDER_TIME_DONE_MSC, ORDER_TYPE_FILLING,
    ORDER_TYPE_TIME, ORDER_MAGIC, ORDER_REASON, ORDER_POSITION_ID, ORDER_POSITION_BY_ID };

enum ENUM_ORDER_PROPERTY_DOUBLE
  { ORDER_VOLUME_INITIAL, ORDER_VOLUME_CURRENT, ORDER_PRICE_OPEN, ORDER_SL, ORDER_TP,
    ORDER_PRICE_CURRENT, ORDER_PRICE_STOPLIMIT };

enum ENUM_ORDER_PROPERTY_STRING { ORDER_SYMBOL, ORDER_COMMENT, ORDER_EXTERNAL_ID };

enum ENUM_ORDER_TYPE
  { ORDER_TYPE_BUY = 0, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT,
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP, ORDER_TYPE_BUY_STOP_LIMIT,
    ORDER_TYPE_SELL_STOP_LIMIT, ORDER_TYPE_CLOSE_BY };

enum ENUM_ORDER_TYPE_FILLING
  { ORDER_FILLING_FOK = 0, ORDER_FILLING_IOC = 1, ORDER_FILLING_BOC = 2, ORDER_FILLING_RETURN = 3 };

enum ENUM_ORDER_TYPE_TIME
  { ORDER_TIME_GTC = 0, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED, ORDER_TIME_SPECIFIED_DAY };

enum ENUM_DEAL_PROPERTY_INTEGER
  { DEAL_TICKET, DEAL_ORDER, DEAL_TIME, DEAL_TIME_MSC, DEAL_TYPE, DEAL_ENTRY, DEAL_MAGIC,
    DEAL_REASON, DEAL_POSITION_ID };

enum ENUM_DEAL_PROPERTY_DOUBLE
  { DEAL_VOLUME, DEAL_PRICE, DEAL_COMMISSION, DEAL_SWAP, DEAL_PROFIT, DEAL_FEE };

enum ENUM_DEAL_PROPERTY_STRING { DEAL_SYMBOL, DEAL_COMMENT, DEAL_EXTERNAL_ID };

enum ENUM_DEAL_TYPE { DEAL_TYPE_BUY = 0, DEAL_TYPE_SELL = 1, DEAL_TYPE_BALANCE };
enum ENUM_DEAL_ENTRY { DEAL_ENTRY_IN = 0, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY };

enum ENUM_TERMINAL_INFO_INTEGER
  { TERMINAL_BUILD, TERMINAL_CONNECTED, TERMINAL_TRADE_ALLOWED, TERMINAL_DLLS_ALLOWED,
    TERMINAL_MAXBARS, TERMINAL_CODEPAGE, TERMINAL_PING_LAST };

enum ENUM_MQL_INFO_INTEGER
  { MQL_TRADE_ALLOWED, MQL_DLLS_ALLOWED, MQL_TESTER, MQL_OPTIMIZATION, MQL_DEBUG,
    MQL_PROGRAM_TYPE, MQL_FORWARD, MQL_VISUAL_MODE };

enum ENUM_TRADE_TRANSACTION_TYPE
  { TRADE_TRANSACTION_ORDER_ADD, TRADE_TRANSACTION_ORDER_UPDATE, TRADE_TRANSACTION_ORDER_DELETE,
    TRADE_TRANSACTION_DEAL_ADD, TRADE_TRANSACTION_DEAL_UPDATE, TRADE_TRANSACTION_DEAL_DELETE,
    TRADE_TRANSACTION_HISTORY_ADD, TRADE_TRANSACTION_HISTORY_UPDATE,
    TRADE_TRANSACTION_HISTORY_DELETE, TRADE_TRANSACTION_POSITION, TRADE_TRANSACTION_REQUEST };

enum ENUM_TRADE_REQUEST_ACTIONS
  { TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY,
    TRADE_ACTION_REMOVE, TRADE_ACTION_CLOSE_BY };

struct MqlTradeRequest
  {
   ENUM_TRADE_REQUEST_ACTIONS action; ulong magic; ulong order; string symbol;
   double volume; double price; double stoplimit; double sl; double tp; ulong deviation;
   ENUM_ORDER_TYPE type; ENUM_ORDER_TYPE_FILLING type_filling; ENUM_ORDER_TYPE_TIME type_time;
   datetime expiration; string comment; ulong position; ulong position_by;
   MqlTradeRequest() : action(TRADE_ACTION_DEAL), magic(0), order(0), volume(0), price(0),
     stoplimit(0), sl(0), tp(0), deviation(0), type(ORDER_TYPE_BUY),
     type_filling(ORDER_FILLING_FOK), type_time(ORDER_TIME_GTC), expiration(0),
     position(0), position_by(0) {}
  };

struct MqlTradeResult
  {
   uint retcode; ulong deal; ulong order; double volume; double price; double bid; double ask;
   string comment; uint request_id; int retcode_external;
   MqlTradeResult() : retcode(0), deal(0), order(0), volume(0), price(0), bid(0), ask(0),
     request_id(0), retcode_external(0) {}
  };

enum ENUM_ORDER_STATE
  { ORDER_STATE_STARTED, ORDER_STATE_PLACED, ORDER_STATE_CANCELED, ORDER_STATE_PARTIAL,
    ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED,
    ORDER_STATE_REQUEST_ADD, ORDER_STATE_REQUEST_MODIFY, ORDER_STATE_REQUEST_CANCEL };

struct MqlTradeTransaction
  {
   ulong deal; ulong order; string symbol; ENUM_TRADE_TRANSACTION_TYPE type;
   ENUM_ORDER_TYPE order_type; ENUM_ORDER_STATE order_state; ENUM_DEAL_TYPE deal_type;
   ENUM_ORDER_TYPE_TIME time_type; datetime time_expiration;
   double price; double price_trigger; double price_sl; double price_tp; double volume;
   ulong position; ulong position_by;
   MqlTradeTransaction() : deal(0), order(0), type(TRADE_TRANSACTION_ORDER_ADD),
     order_type(ORDER_TYPE_BUY), order_state(ORDER_STATE_STARTED), deal_type(DEAL_TYPE_BUY),
     time_type(ORDER_TIME_GTC), time_expiration(0), price(0), price_trigger(0), price_sl(0),
     price_tp(0), volume(0), position(0), position_by(0) {}
  };

//-------------------------------------------------------------------
// Return codes and misc constants
//-------------------------------------------------------------------
#define INVALID_HANDLE (-1)
#define CP_UTF8 65001
#define INIT_SUCCEEDED 0
#define INIT_FAILED (-1)
#define INIT_PARAMETERS_INCORRECT (-2)
#define LOG_LEVEL_NO 0
#define LOG_LEVEL_ERRORS 1
#define LOG_LEVEL_ALL 2
#define ULONG_MAX_MQL 0xFFFFFFFFFFFFFFFFULL

//-------------------------------------------------------------------
// Builtin functions
//-------------------------------------------------------------------
template <class... A> void Print(A &&...) {}
template <class... A> void PrintFormat(const string &, A &&...) {}
template <class... A> string StringFormat(const string &, A &&...) { return string(); }

int    StringLen(const string &);
ushort StringGetCharacter(const string &, int);
int    StringFind(const string &, const string &, int start = 0);
string StringSubstr(const string &, int start, int count = -1);
int    StringTrimLeft(string &);
int    StringTrimRight(string &);
int    StringSplit(const string &, ushort separator, MqlArray<string> &);
double StringToDouble(const string &);
long   StringToInteger(const string &);
string ShortToString(ushort);
string IntegerToString(long value, int str_len = 0, ushort fill = ' ');
string DoubleToString(double value, int digits = 8);
string EnumToString(ENUM_TRADE_TRANSACTION_TYPE);
int    StringToCharArray(const string &, MqlArray<uchar> &, int start = 0,
                         int count = WHOLE_ARRAY, uint codepage = CP_UTF8);
string CharArrayToString(const MqlArray<uchar> &, int start = 0, int count = -1,
                         uint codepage = CP_UTF8);

// Compared in the common type so that a mixed signed/unsigned call does not
// raise a warning MQL5 would never raise -- the promotion is the shim's, not
// the source's.
template <class A, class B>
typename std::common_type<A, B>::type MathMin(A a, B b)
  {
   typedef typename std::common_type<A, B>::type C;
   return (C)a < (C)b ? (C)a : (C)b;
  }
template <class A, class B>
typename std::common_type<A, B>::type MathMax(A a, B b)
  {
   typedef typename std::common_type<A, B>::type C;
   return (C)a > (C)b ? (C)a : (C)b;
  }
double MathAbs(double);
double MathFloor(double);
double MathRound(double);
double NormalizeDouble(double value, int digits);

bool   SymbolSelect(const string &, bool enable);
long   SymbolInfoInteger(const string &, ENUM_SYMBOL_INFO_INTEGER);
double SymbolInfoDouble(const string &, ENUM_SYMBOL_INFO_DOUBLE);
string SymbolInfoString(const string &, ENUM_SYMBOL_INFO_STRING);
bool   SymbolInfoTick(const string &, MqlTick &);

long   AccountInfoInteger(ENUM_ACCOUNT_INFO_INTEGER);
double AccountInfoDouble(ENUM_ACCOUNT_INFO_DOUBLE);
string AccountInfoString(ENUM_ACCOUNT_INFO_STRING);

long   TerminalInfoInteger(ENUM_TERMINAL_INFO_INTEGER);
long   MQLInfoInteger(ENUM_MQL_INFO_INTEGER);

datetime TimeCurrent();
datetime TimeGMT();
datetime TimeLocal();

datetime iTime(const string &, ENUM_TIMEFRAMES, int shift);
double   iClose(const string &, ENUM_TIMEFRAMES, int shift);
double   iOpen(const string &, ENUM_TIMEFRAMES, int shift);
double   iHigh(const string &, ENUM_TIMEFRAMES, int shift);
double   iLow(const string &, ENUM_TIMEFRAMES, int shift);
int      CopyRates(const string &, ENUM_TIMEFRAMES, int start, int count, MqlArray<MqlRates> &);

int    PositionsTotal();
ulong  PositionGetTicket(int index);
bool   PositionSelectByTicket(ulong ticket);
bool   PositionSelect(const string &);
long   PositionGetInteger(ENUM_POSITION_PROPERTY_INTEGER);
double PositionGetDouble(ENUM_POSITION_PROPERTY_DOUBLE);
string PositionGetString(ENUM_POSITION_PROPERTY_STRING);

int    OrdersTotal();
ulong  OrderGetTicket(int index);
bool   OrderSelect(ulong ticket);
long   OrderGetInteger(ENUM_ORDER_PROPERTY_INTEGER);
double OrderGetDouble(ENUM_ORDER_PROPERTY_DOUBLE);
string OrderGetString(ENUM_ORDER_PROPERTY_STRING);

bool   HistorySelect(datetime from, datetime to);
int    HistoryDealsTotal();
ulong  HistoryDealGetTicket(int index);
long   HistoryDealGetInteger(ulong ticket, ENUM_DEAL_PROPERTY_INTEGER);
double HistoryDealGetDouble(ulong ticket, ENUM_DEAL_PROPERTY_DOUBLE);
string HistoryDealGetString(ulong ticket, ENUM_DEAL_PROPERTY_STRING);

int  SocketCreate(uint flags = 0);
void SocketClose(int handle);
bool SocketConnect(int handle, const string &server, uint port, uint timeout);
bool SocketIsConnected(int handle);
uint SocketIsReadable(int handle);
int  SocketRead(int handle, MqlArray<uchar> &, uint count, uint timeout);
int  SocketSend(int handle, const MqlArray<uchar> &, uint count);

bool EventSetMillisecondTimer(int milliseconds);
bool EventSetTimer(int seconds);
void EventKillTimer();
int  GetLastError();
void ResetLastError();
