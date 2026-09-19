// PunchPoint scanner - Arduino Uno/Nano + R307 / AS608 / ZFM-20 fingerprint module.
//
// Wiring
//   sensor VCC  -> 5V  (some modules want 3.3V, check yours)
//   sensor GND  -> GND
//   sensor TX   -> D2
//   sensor RX   -> D3   (through a 1k + 2k divider if the module is 3.3V)
//
// Install the "Adafruit Fingerprint Sensor Library" from the Library Manager,
// upload this sketch, then plug the board into the PunchPoint server by USB and
// pick its port in Settings. One line goes out per event, which is all the
// server needs.

#include <Adafruit_Fingerprint.h>
#include <SoftwareSerial.h>

SoftwareSerial fpSerial(2, 3);
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&fpSerial);

const unsigned long REPEAT_LOCKOUT = 4000;   // ignore the same finger for 4s
unsigned long lastScan = 0;

void setup() {
  Serial.begin(9600);            // must match the speed set in PunchPoint
  while (!Serial) { }
  finger.begin(57600);
  delay(100);
  if (finger.verifyPassword()) Serial.println("READY");
  else Serial.println("LOG fingerprint module not answering - check the wiring");
}

void loop() {
  if (Serial.available()) handleCommand(readLine());
  scanOnce();
}

String readLine() {
  String line = Serial.readStringUntil('\n');
  line.trim();
  return line;
}

void handleCommand(String cmd) {
  cmd.toUpperCase();
  if (cmd == "PING") Serial.println("READY");
  else if (cmd.startsWith("ENROLL")) enroll(cmd.substring(6).toInt());
  else if (cmd.startsWith("DELETE")) {
    int id = cmd.substring(6).toInt();
    Serial.println(finger.deleteModel(id) == FINGERPRINT_OK ? "DELETED " + String(id) : "LOG delete failed");
  }
}

void scanOnce() {
  if (finger.getImage() != FINGERPRINT_OK) return;
  if (finger.image2Tz() != FINGERPRINT_OK) return;
  if (finger.fingerFastSearch() != FINGERPRINT_OK) {
    if (millis() - lastScan > REPEAT_LOCKOUT) { Serial.println("NOMATCH"); lastScan = millis(); }
    return;
  }
  if (millis() - lastScan < REPEAT_LOCKOUT) return;
  lastScan = millis();
  Serial.print("SCAN ");
  Serial.println(finger.fingerID);          // -> the scanner slot PunchPoint stores
}

void enroll(int id) {
  if (id <= 0) { Serial.println("ENROLL FAIL bad slot number"); return; }
  Serial.println("ENROLL PLACE");
  if (!capture(1)) return;
  Serial.println("ENROLL LIFT");
  delay(1200);
  while (finger.getImage() != FINGERPRINT_NOFINGER) delay(100);
  Serial.println("ENROLL AGAIN");
  if (!capture(2)) return;
  if (finger.createModel() != FINGERPRINT_OK) { Serial.println("ENROLL FAIL the two scans did not match"); return; }
  if (finger.storeModel(id) != FINGERPRINT_OK) { Serial.println("ENROLL FAIL could not save to the scanner"); return; }
  Serial.print("ENROLL OK ");
  Serial.println(id);
}

bool capture(int slot) {
  unsigned long until = millis() + 20000;
  while (millis() < until) {
    if (finger.getImage() == FINGERPRINT_OK) {
      if (finger.image2Tz(slot) == FINGERPRINT_OK) return true;
      Serial.println("ENROLL FAIL that scan was too blurry");
      return false;
    }
    delay(80);
  }
  Serial.println("ENROLL FAIL timed out waiting for a finger");
  return false;
}
