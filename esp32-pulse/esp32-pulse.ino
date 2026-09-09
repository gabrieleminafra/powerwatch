/*
 * Powerwatch — pulse
 *
 * ESP32 su una presa NON protetta da UPS: manda un pacchetto UDP al watchdog
 * ogni PULSE_INTERVAL_MS. Se manca la corrente si spegne, i pacchetti cessano,
 * e il watchdog (sotto UPS) se ne accorge.
 *
 * La configurazione (WiFi + indirizzo del watchdog) NON e' compilata qui
 * dentro: al primo avvio la board apre un access point e te la fa inserire dal
 * telefono. Cosi' la password del WiFi non finisce nel sorgente, e cambiare
 * router non richiede di riflashare.
 *
 * Il watchdog risponde "ack" a ogni pulse: il LED dice se l'indirizzo che hai
 * configurato e' quello giusto.
 *   - un lampeggio breve per pulse .... ack ricevuto, tutto a posto
 *   - tre lampeggi rapidi ............ nessuna risposta: IP/porta sbagliati,
 *                                      o il server non e' in ascolto
 *
 * Per riconfigurare, due strade:
 *   - tieni premuto BOOT per 3 s CON LA BOARD ACCESA (non durante il reset:
 *     GPIO0 basso all'avvio manda l'ESP32 nel bootloader e lo sketch non parte)
 *   - oppure via seriale a 115200: scrivi "help"
 *
 * Librerie: WiFiManager (tzapu)
 */

#include <Preferences.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiUdp.h>

static const char* SETUP_AP_NAME = "powerwatch-setup";
static const char* SETUP_AP_PASS = "powerwatch";   // cambiala se vuoi

static const uint32_t PULSE_INTERVAL_MS = 10000;   // un pulse ogni 10 s
static const uint32_t ACK_TIMEOUT_MS    = 400;     // attesa della risposta
static const uint32_t PORTAL_TIMEOUT_S  = 300;     // poi riavvia e ritenta
static const uint32_t BOOT_HOLD_MS      = 3000;    // pressione per riconfigurare
static const uint16_t LOCAL_UDP_PORT    = 9998;    // porta da cui inviamo

static const int PIN_LED  = 2;   // LED di bordo sulla maggior parte dei devkit
static const int PIN_BOOT = 0;   // pulsante BOOT

Preferences prefs;
WiFiUDP udp;

String cfgHost, cfgPort, cfgToken;
bool shouldSave = false;
uint32_t lastPulse = 0;
uint32_t bootPressedAt = 0;
bool lastAcked = false;

void saveParamsCallback() { shouldSave = true; }

void blink(int times, int on, int off) {
  for (int i = 0; i < times; i++) {
    digitalWrite(PIN_LED, HIGH); delay(on);
    digitalWrite(PIN_LED, LOW);  delay(off);
  }
}

// ---------- configurazione ----------
void loadConfig() {
  prefs.begin("powerwatch", true);
  cfgHost  = prefs.getString("host", "192.168.1.10");
  cfgPort  = prefs.getString("port", "9999");
  cfgToken = prefs.getString("token", "cambiami");
  prefs.end();
}

void storeConfig(const String& host, const String& port, const String& token) {
  prefs.begin("powerwatch", false);
  prefs.putString("host", host);
  prefs.putString("port", port);
  prefs.putString("token", token);
  prefs.end();
}

void printConfig() {
  Serial.printf("[cfg] watchdog  %s:%s\n", cfgHost.c_str(), cfgPort.c_str());
  Serial.printf("[cfg] token     %s\n", cfgToken.c_str());
  Serial.printf("[cfg] wifi      %s (IP %s)\n",
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
}

// Cancella WiFi e configurazione, poi riavvia: al riavvio setup() non trova
// credenziali e riapre il portale.
void wipeAndRestart() {
  Serial.println("[cfg] cancello configurazione e riavvio nel portale");
  WiFiManager wm;
  wm.resetSettings();
  prefs.begin("powerwatch", false);
  prefs.clear();
  prefs.end();
  blink(6, 80, 80);
  delay(300);
  ESP.restart();
}

// ---------- console seriale ----------
void runCommand(String line) {
  line.trim();
  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String arg = (sp < 0) ? ""   : line.substring(sp + 1);
  arg.trim();

  if (cmd == "help") {
    Serial.println("comandi: show | host <ip> | port <n> | token <s> | portal | reset");
  } else if (cmd == "show") {
    printConfig();
  } else if (cmd == "host" && arg.length()) {
    storeConfig(arg, cfgPort, cfgToken); loadConfig(); printConfig();
  } else if (cmd == "port" && arg.length()) {
    storeConfig(cfgHost, arg, cfgToken); loadConfig(); printConfig();
  } else if (cmd == "token" && arg.length()) {
    storeConfig(cfgHost, cfgPort, arg); loadConfig(); printConfig();
  } else if (cmd == "portal" || cmd == "reset") {
    wipeAndRestart();
  } else {
    Serial.printf("[serial] comando sconosciuto: '%s' (prova 'help')\n", cmd.c_str());
  }
}

void handleSerial() {
  static String buf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) { runCommand(buf); buf = ""; }
    } else if (buf.length() < 120) {
      buf += c;
    }
  }
}

// BOOT tenuto premuto A BOARD ACCESA per 3 s = riconfigura.
void handleBootButton() {
  if (digitalRead(PIN_BOOT) == LOW) {
    if (bootPressedAt == 0) {
      bootPressedAt = millis();
      Serial.println("[cfg] BOOT premuto, tieni 3 s per riconfigurare...");
    } else if (millis() - bootPressedAt >= BOOT_HOLD_MS) {
      wipeAndRestart();
    }
  } else {
    bootPressedAt = 0;
  }
}

// ---------- pulse ----------
void sendPulse() {
  udp.beginPacket(cfgHost.c_str(), cfgPort.toInt());
  udp.print(cfgToken);
  udp.endPacket();

  // Il watchdog risponde "ack": e' la conferma che l'indirizzo e' giusto.
  bool acked = false;
  uint32_t t0 = millis();
  while (millis() - t0 < ACK_TIMEOUT_MS) {
    int len = udp.parsePacket();
    if (len > 0) {
      char b[8] = {0};
      udp.read(b, min(len, 7));
      if (strncmp(b, "ack", 3) == 0) { acked = true; break; }
    }
    delay(10);
  }

  if (acked) {
    blink(1, 30, 0);
    if (!lastAcked) Serial.printf("[pulse] %s:%s risponde\n", cfgHost.c_str(), cfgPort.c_str());
  } else {
    blink(3, 40, 60);
    Serial.printf("[pulse] nessuna risposta da %s:%s — indirizzo giusto? server acceso?\n",
                  cfgHost.c_str(), cfgPort.c_str());
  }
  lastAcked = acked;
}

// ---------- avvio ----------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BOOT, INPUT_PULLUP);
  delay(300);
  Serial.println("\n[Powerwatch] pulse — scrivi 'help' per i comandi");

  loadConfig();

  WiFiManager wm;
  WiFiManagerParameter pHost("host", "IP del watchdog", cfgHost.c_str(), 40);
  WiFiManagerParameter pPort("port", "Porta UDP", cfgPort.c_str(), 6);
  WiFiManagerParameter pToken("token", "Token (PW_TOKEN)", cfgToken.c_str(), 64);
  wm.addParameter(&pHost);
  wm.addParameter(&pPort);
  wm.addParameter(&pToken);
  wm.setSaveParamsCallback(saveParamsCallback);
  wm.setConfigPortalTimeout(PORTAL_TIMEOUT_S);
  wm.setConnectTimeout(20);

  // Con credenziali salvate si collega e basta; altrimenti apre il portale.
  if (!wm.autoConnect(SETUP_AP_NAME, SETUP_AP_PASS)) {
    Serial.println("[wifi] connessione fallita, riavvio");
    delay(1000);
    ESP.restart();
  }

  if (shouldSave) {
    storeConfig(pHost.getValue(), pPort.getValue(), pToken.getValue());
    loadConfig();
  }

  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  udp.begin(LOCAL_UDP_PORT);   // serve per poter ricevere l'ack
  printConfig();
}

void loop() {
  handleSerial();
  handleBootButton();

  // Dopo un blackout il router (sotto UPS) e' gia' su: qui si riaggancia.
  if (WiFi.status() != WL_CONNECTED) {
    digitalWrite(PIN_LED, LOW);
    delay(100);
    return;
  }

  if (millis() - lastPulse >= PULSE_INTERVAL_MS) {
    lastPulse = millis();
    sendPulse();
  }

  delay(50);
}
