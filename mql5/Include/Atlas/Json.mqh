//+------------------------------------------------------------------+
//| Atlas/Json.mqh                                                    |
//| Minimal JSON writer and reader for the ATLAS bridge protocol.     |
//|                                                                   |
//| Deliberately small. The protocol (docs/PROTOCOL.md) uses flat     |
//| objects with scalar values, one array of arrays for bars, and one |
//| array of objects for positions. A general-purpose JSON library    |
//| would be more code to review and more surface to get wrong, and   |
//| this runs inside a process that sends orders.                     |
//+------------------------------------------------------------------+
#property strict

//+------------------------------------------------------------------+
//| Escape a string for embedding in JSON.                            |
//| Control characters are escaped rather than dropped: a broker      |
//| comment containing a stray byte must not be able to produce a     |
//| frame the Python side cannot parse.                               |
//+------------------------------------------------------------------+
string JsonEscape(const string value)
  {
   string out = "";
   int n = StringLen(value);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(value, i);
      switch(c)
        {
         case '"':  out += "\\\"";  break;
         case '\\': out += "\\\\";  break;
         case '\n': out += "\\n";   break;
         case '\r': out += "\\r";   break;
         case '\t': out += "\\t";   break;
         default:
            if(c < 0x20)
               out += StringFormat("\\u%04x", c);
            else
               out += ShortToString(c);
        }
     }
   return out;
  }

string JsonStr(const string key, const string value)
  {
   return "\"" + JsonEscape(key) + "\":\"" + JsonEscape(value) + "\"";
  }

string JsonNum(const string key, const double value, const int digits = 8)
  {
   return "\"" + JsonEscape(key) + "\":" + DoubleToString(value, digits);
  }

string JsonInt(const string key, const long value)
  {
   return "\"" + JsonEscape(key) + "\":" + IntegerToString(value);
  }

string JsonBool(const string key, const bool value)
  {
   return "\"" + JsonEscape(key) + "\":" + (value ? "true" : "false");
  }

string JsonNull(const string key)
  {
   return "\"" + JsonEscape(key) + "\":null";
  }

//+------------------------------------------------------------------+
//| Price fields are emitted as null when zero.                       |
//| MT5 reports "no stop loss" as 0.0, which is not a price. Sending  |
//| it as 0.0 would let the engine believe a position is protected    |
//| when it is not.                                                   |
//+------------------------------------------------------------------+
string JsonPrice(const string key, const double value, const int digits)
  {
   if(value == 0.0)
      return JsonNull(key);
   return "\"" + JsonEscape(key) + "\":" + DoubleToString(value, digits);
  }

string JsonObject(const string &parts[], const int count)
  {
   string out = "{";
   for(int i = 0; i < count; i++)
     {
      if(i > 0)
         out += ",";
      out += parts[i];
     }
   return out + "}";
  }

//+------------------------------------------------------------------+
//| Reader.                                                           |
//|                                                                   |
//| Scans for "key": and returns the raw token that follows. This is  |
//| a scanner, not a parser: it does not validate nesting. That is    |
//| acceptable because the peer is ATLAS, whose frames are generated  |
//| by a tested codec -- but it does mean the EA must never treat a   |
//| missing key as a valid default for anything that moves money,     |
//| which is why every extractor below has an explicit `found` flag.  |
//+------------------------------------------------------------------+
bool JsonFind(const string json, const string key, string &raw_out)
  {
   string needle = "\"" + key + "\":";
   int at = StringFind(json, needle, 0);
   if(at < 0)
      return false;
   int i = at + StringLen(needle);
   int n = StringLen(json);
   while(i < n && StringGetCharacter(json, i) == ' ')
      i++;
   if(i >= n)
      return false;

   ushort first = StringGetCharacter(json, i);
   int start = i;
   if(first == '"')
     {
      i++;
      string out = "";
      while(i < n)
        {
         ushort c = StringGetCharacter(json, i);
         if(c == '\\' && i + 1 < n)
           {
            ushort esc = StringGetCharacter(json, i + 1);
            if(esc == 'n')      out += "\n";
            else if(esc == 't') out += "\t";
            else if(esc == 'r') out += "\r";
            else if(esc == 'u') { i += 6; continue; }
            else                out += ShortToString(esc);
            i += 2;
            continue;
           }
         if(c == '"')
            break;
         out += ShortToString(c);
         i++;
        }
      raw_out = out;
      return true;
     }

   int depth = 0;
   while(i < n)
     {
      ushort c = StringGetCharacter(json, i);
      if(c == '{' || c == '[')
         depth++;
      else if(c == '}' || c == ']')
        {
         if(depth == 0)
            break;
         depth--;
        }
      else if(c == ',' && depth == 0)
         break;
      i++;
     }
   raw_out = StringSubstr(json, start, i - start);
   StringTrimLeft(raw_out);
   StringTrimRight(raw_out);
   return true;
  }

string JsonGetString(const string json, const string key, const string fallback = "")
  {
   string raw;
   if(!JsonFind(json, key, raw))
      return fallback;
   return raw;
  }

//+------------------------------------------------------------------+
//| Numeric extraction returns `found` separately from the value.     |
//| A caller must be able to distinguish "the key was absent" from    |
//| "the value was zero" -- for a stop loss those mean opposite       |
//| things.                                                           |
//+------------------------------------------------------------------+
bool JsonGetDouble(const string json, const string key, double &value_out)
  {
   string raw;
   if(!JsonFind(json, key, raw))
      return false;
   if(raw == "null" || raw == "")
      return false;
   value_out = StringToDouble(raw);
   return true;
  }

bool JsonGetLong(const string json, const string key, long &value_out)
  {
   string raw;
   if(!JsonFind(json, key, raw))
      return false;
   if(raw == "null" || raw == "")
      return false;
   value_out = StringToInteger(raw);
   return true;
  }

double JsonGetDoubleOr(const string json, const string key, const double fallback)
  {
   double v;
   return JsonGetDouble(json, key, v) ? v : fallback;
  }

long JsonGetLongOr(const string json, const string key, const long fallback)
  {
   long v;
   return JsonGetLong(json, key, v) ? v : fallback;
  }

//+------------------------------------------------------------------+
//| Split a top-level comma-separated list of strings.                |
//| Used only for the {"symbols":["A","B"]} argument.                 |
//+------------------------------------------------------------------+
int JsonStringArray(const string raw, string &out[])
  {
   ArrayResize(out, 0);
   int n = StringLen(raw);
   string current = "";
   bool inside = false;
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(raw, i);
      if(c == '"')
        {
         if(inside)
           {
            int k = ArraySize(out);
            ArrayResize(out, k + 1);
            out[k] = current;
            current = "";
           }
         inside = !inside;
         continue;
        }
      if(inside)
         current += ShortToString(c);
     }
   return ArraySize(out);
  }
//+------------------------------------------------------------------+
