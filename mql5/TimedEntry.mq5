//+------------------------------------------------------------------+
//| TimedEntry.mq5                                                   |
//| Opens ONE market position at an exact time (PC clock by default).|
//| Run one copy per MT5 terminal:                                   |
//|   Terminal A (account 1) -> Direction = Buy                      |
//|   Terminal B (account 2) -> Direction = Sell                     |
//| Both read the same PC clock, so they fire in the same second.    |
//+------------------------------------------------------------------+
#property copyright   "William"
#property version     "1.00"
#property description "Opens one Buy or Sell at a set time with a money-based TP and no SL."

#include <Trade\Trade.mqh>

enum ENUM_DIRECTION
  {
   DIR_BUY  = 0, // Buy
   DIR_SELL = 1  // Sell
  };

input ENUM_DIRECTION InpDirection     = DIR_BUY;   // Direction for THIS account
input double         InpLots          = 0.06;      // Lot size
input double         InpTakeProfitUSD = 150.0;     // Take profit in account money (0 = no TP)
input int            InpHour          = 14;        // Open hour
input int            InpMinute        = 29;        // Open minute
input int            InpSecond        = 45;        // Open second
input bool           InpUseLocalTime  = true;      // true = PC clock, false = broker server time
input int            InpMaxLateSec    = 2;         // Still open if the terminal is up to N seconds late
input string         InpOnlyDate      = "";        // Only on this date, e.g. 2026.11.20 (empty = every day)
input int            InpSlippagePts   = 300;       // Max slippage in points
input ulong          InpMagic         = 20261115;  // Magic number

CTrade   trade;
datetime g_lastTradeDay = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippagePts);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);

   double vmin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(InpLots < vmin || InpLots > vmax ||
      MathAbs(InpLots / vstep - MathRound(InpLots / vstep)) > 1e-6)
     {
      PrintFormat("Invalid lot %.2f for %s (min %.2f, max %.2f, step %.2f)",
                  InpLots, _Symbol, vmin, vmax, vstep);
      return INIT_PARAMETERS_INCORRECT;
     }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("WARNING: the Algo Trading button in the toolbar is OFF.");
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("WARNING: 'Allow Algo Trading' is unchecked in the EA settings.");

   // Show what the TP and margin will look like right now
   MqlTick tick;
   if(SymbolInfoTick(_Symbol, tick))
     {
      bool   isBuy = (InpDirection == DIR_BUY);
      double price = isBuy ? tick.ask : tick.bid;
      double tp    = (InpTakeProfitUSD > 0) ? TpPrice(isBuy, price) : 0;
      double margin = 0;
      OrderCalcMargin(isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, InpLots, price, margin);
      PrintFormat("%s %.2f lot %s | price %s | TP %s (distance %.2f) | margin needed %.2f | free margin %.2f",
                  isBuy ? "BUY" : "SELL", InpLots, _Symbol,
                  DoubleToString(price, _Digits), DoubleToString(tp, _Digits),
                  MathAbs(tp - price), margin, AccountInfoDouble(ACCOUNT_MARGIN_FREE));
     }

   EventSetMillisecondTimer(20);   // check the clock every 20 ms
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   datetime now = InpUseLocalTime ? TimeLocal() : TimeTradeServer();
   MqlDateTime t;
   TimeToStruct(now, t);

   datetime day    = now - (now % 86400);
   int      nowSec = t.hour * 3600 + t.min * 60 + t.sec;
   int      target = InpHour * 3600 + InpMinute * 60 + InpSecond;

   static int lastShown = -1;
   if(t.sec != lastShown)
     {
      lastShown = t.sec;
      Comment(StringFormat("TimedEntry  %s %.2f lot  TP %.2f\nNow (%s): %s\nTarget: %02d:%02d:%02d%s\nStatus: %s",
                           InpDirection == DIR_BUY ? "BUY" : "SELL", InpLots, InpTakeProfitUSD,
                           InpUseLocalTime ? "PC" : "server", TimeToString(now, TIME_DATE | TIME_SECONDS),
                           InpHour, InpMinute, InpSecond,
                           InpOnlyDate == "" ? " every day" : " on " + InpOnlyDate,
                           g_lastTradeDay == day ? "done for today" : "waiting"));
     }

   if(g_lastTradeDay == day)
      return;
   if(InpOnlyDate != "" && day != StringToTime(InpOnlyDate))
      return;
   if(nowSec < target || nowSec > target + InpMaxLateSec)
      return;

   g_lastTradeDay = day;           // mark first, so it can never fire twice
   if(HasOurPosition())
     {
      Print("Position with this magic number is already open - skipping.");
      return;
     }
   OpenTrade();
  }

//+------------------------------------------------------------------+
void OpenTrade()
  {
   bool            isBuy = (InpDirection == DIR_BUY);
   ENUM_ORDER_TYPE type  = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   for(int attempt = 1; attempt <= 3; attempt++)
     {
      MqlTick tick;
      if(!SymbolInfoTick(_Symbol, tick))
        {
         Sleep(20);
         continue;
        }
      double price = isBuy ? tick.ask : tick.bid;
      double tp    = (InpTakeProfitUSD > 0) ? TpPrice(isBuy, price) : 0;

      ulong t0 = GetMicrosecondCount();
      bool ok = isBuy ? trade.Buy(InpLots, _Symbol, price, 0, tp, "TimedEntry")
                      : trade.Sell(InpLots, _Symbol, price, 0, tp, "TimedEntry");
      uint rc = trade.ResultRetcode();

      if(ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED))
        {
         PrintFormat("%s %.2f %s filled at %s in %.0f ms (local %s)",
                     isBuy ? "BUY" : "SELL", InpLots, _Symbol,
                     DoubleToString(trade.ResultPrice(), _Digits),
                     (GetMicrosecondCount() - t0) / 1000.0,
                     TimeToString(TimeLocal(), TIME_SECONDS));
         AlignTpToFill(isBuy);
         return;
        }

      PrintFormat("Attempt %d failed: %u %s", attempt, rc, trade.ResultRetcodeDescription());
      if(rc != TRADE_RETCODE_REQUOTE && rc != TRADE_RETCODE_PRICE_CHANGED && rc != TRADE_RETCODE_PRICE_OFF)
         return;                   // e.g. no money / market closed: retrying won't help
     }
  }

//+------------------------------------------------------------------+
//| TP price that gives InpTakeProfitUSD profit from openPrice       |
//+------------------------------------------------------------------+
double TpPrice(bool isBuy, double openPrice)
  {
   ENUM_ORDER_TYPE type  = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double probe   = 1.0;                                   // 1.0 price unit (e.g. $1 on gold)
   double closeP  = isBuy ? openPrice + probe : openPrice - probe;
   double profit  = 0;
   double perUnit = 0;

   if(OrderCalcProfit(type, _Symbol, InpLots, openPrice, closeP, profit) && profit > 0)
      perUnit = profit / probe;
   else
     {
      double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tv <= 0 || ts <= 0)
         return 0;
      perUnit = InpLots * tv / ts;
     }

   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double dist     = InpTakeProfitUSD / perUnit;
   if(tickSize > 0)
      dist = MathCeil(dist / tickSize) * tickSize;

   return NormalizeDouble(isBuy ? openPrice + dist : openPrice - dist, _Digits);
  }

//+------------------------------------------------------------------+
//| After the fill, move TP so profit is measured from the real fill |
//+------------------------------------------------------------------+
void AlignTpToFill(bool isBuy)
  {
   if(InpTakeProfitUSD <= 0)
      return;

   for(int wait = 0; wait < 20; wait++)          // position can take a moment to appear
     {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
            (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic)
            continue;

         double open  = PositionGetDouble(POSITION_PRICE_OPEN);
         double curTp = PositionGetDouble(POSITION_TP);
         double want  = TpPrice(isBuy, open);
         if(MathAbs(want - curTp) >= SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE))
           {
            if(trade.PositionModify(ticket, 0, want))
               PrintFormat("TP aligned to fill: %s", DoubleToString(want, _Digits));
            else
               PrintFormat("TP modify failed: %u %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
           }
         return;
        }
      Sleep(50);
     }
  }

//+------------------------------------------------------------------+
bool HasOurPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return true;
     }
   return false;
  }
//+------------------------------------------------------------------+
