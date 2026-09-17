# DarkFlash 케이스 화면(LCD) (USB 1D6B:0148) 통신 프로토콜 분석

- 대상: `C:\Program Files\DarkFlash\DarkFlash.exe` v1.5.3 (Electron 25.9.7 / V8 11.4.183.29)
- 장치: `USB\VID_1D6B&PID_0148`, "USB Display Device", HID vendor-defined (usagePage 0xFF00, usage 1), 펌웨어 RTOS, app/firmware V1.0.1, sdk V1.2.0, hw V1.0
- 분석일: 2026-09-17, 정적 분석만 수행 (장치 열기, USB 송신, DarkFlash 수정/재시작 없음)
- 표기: **[확인]** = 바이트코드와 로그 양쪽 또는 로그 3,885개 프레임으로 검증됨, **[코드]** = 바이트코드에서 읽음(실측 없음), **[추정]** = 간접 증거, **[미확인]** = 모름

---

## 0. 요약

**대체 프로그램 제작 가능성: 높음.**

- 전송은 **node-hid**(hidapi) 기반 **HID Output Report(Report ID 0, 1024바이트)** 하나뿐입니다. ADB, 전용 DLL, 암호화, 인증은 없습니다.
- 제어 메시지는 **HTTP 비슷한 텍스트**(`POST brightness 1\r\nSeqNumber=..`)를 `0x5A` 프레임으로 감싼 형태입니다. 로그에 남은 요청 프레임 **3,885개를 제 인코더로 다시 만들어 전부 바이트 단위로 일치**했습니다(`verify_codec.py`).
- 화면은 **PC가 렌더링한 JPEG(품질 85, 최대 140KB, 960x480 추정)**를 **1000바이트 블록 단위 TLV**로 쪼개 HID 리포트로 보냅니다. 블록마다 ACK를 기다리지 않습니다.
- DarkFlash의 CPU 부하 원인: 숨겨진 오프스크린 Chromium 창을 30fps로 렌더링하고, **내용이 바뀌지 않아도 마지막 JPEG를 30fps 틱마다 계속 다시 보냅니다**(프레임당 HID 쓰기 약 83회).
- **"센서 값만 보내고 장치가 그리는" 모드는 이 빌드에서 쓰이지 않습니다.** 명령 팩토리에 `sysinfoDisplay`가 있지만 호출하는 코드가 없고 본문 형식도 알 수 없습니다. 장치 내장 테마(`presetThemeId`)는 PC 없이 표시되지만 센서 값을 보여 주는지는 확인하지 못했습니다.

**필요한 최소 명령 세트 (대체 프로그램 기준)**

| 순서 | 동작 | 비고 |
|---|---|---|
| 1 | HID 열기: VID 0x1D6B, PID 0x0148, usagePage 0xFF00 | node-hid `HID(path)` 방식 |
| 2 | `POST conn` (본문 없음) → 응답 JSON으로 밝기, 회전, 버전 확인 | 핸드셰이크 |
| 3 | `POST power {"event":"resume"}` | 깨우기 |
| 4 | (선택) `STATE all {"heartbeat":1}` | DarkFlash는 실시간 전송 전에 1회 보냄 |
| 5 | `POST realtimeDisplay {"enable":true}` | 실시간 모드 켜기 |
| 6 | JPEG 프레임을 TLV 블록으로 전송 | **변경될 때만 또는 1~2초마다** 보내면 CPU 부하가 거의 없음 |
| 7 | 잠금/절전 시 `realtimeDisplay {"enable":false}` 다음 `power {"event":"suspend"}`, 해제 시 반대 순서 | DarkFlash 동작과 같음 |
| 옵션 | `brightness {"value":0-100}`, `rotate {"degree":0/90/180/270}` | |

---

## 1. 분석 방법 (재현용)

1. `app.asar`를 자체 파서로 풀었습니다(`extract_asar.py`). 메인 프로세스는 **bytenode로 컴파일된 V8 바이트코드**(`out/main/index.jsc`, 628KB)여서 소스가 없습니다.
2. 같은 V8을 쓰는 **공식 Electron 25.9.7 바이너리를 GitHub에서 스크래치패드로 내려받아** `ELECTRON_RUN_AS_NODE`로 실행했습니다. `vm.Script(cachedData)`로 **역직렬화만 하고 실행은 하지 않았습니다**(`load_snap2.js`: `runInThisContext` 없음, 앱 코드 0줄 실행). DarkFlash.exe 자체는 실행하지 않았습니다.
   - `--log-function-events --log-code --log-code-disassemble`로 함수 1,568개의 바이트코드 디스어셈블리를 얻었습니다.
   - 같은 프로세스에서 `HeapProfiler.takeHeapSnapshot(captureNumericValue)`으로 각 함수의 상수 풀(문자열, 숫자)을 얻었습니다.
   - 둘을 소스 위치 트리 + (이름, 바이트코드 길이, 상수풀 크기) 기준으로 1,568/1,568 매칭했습니다(`build2.py`). 결과는 `main_decomp.txt`(78k줄)입니다.
   - 한계: 오브젝트 리터럴 보일러플레이트 안의 Smi 값은 스냅샷에 안 나와서, 필요한 값은 jsc 원시 바이트에서 직접 디코딩했습니다(아래 표에 표시).
3. `%APPDATA%\DarkFlash\logs\*.log`(읽기 전용 복사)에 DarkFlash가 남긴 요청/응답 hex 덤프로 교차 검증했습니다(`logmsgs.py`, `verify_codec.py`).

인용 표기: `index.jsc fn <이름>@<소스오프셋>` = 원본 번들 소스(`evalmachine.<anonymous>:1:<오프셋>`)의 함수이며, `L<n>`은 `main_decomp.txt`의 줄 번호입니다.

---

## 2. 장치 / 전송 계층

| 항목 | 값 | 근거 |
|---|---|---|
| 라이브러리 | `node-hid` 3.1.2 (hidapi, `HID.devicesAsync(vid,pid)` 후 `new HID.HID(path)`) | package.json; `index.jsc fn Pv@219047 L70554`, `fn @219303` ("hid device; index", `HID` 생성) |
| 기타 라이브러리 | `serialport`(Linux 계열 장치용), `appium-adb`(Android/Linux 계열, `MAIN_VITE_ENDABLE_ADB="false"`), `ffi-napi`(user32 등 OS API) | 최상위 상수풀 577-625; `openGraphWindow` 환경 객체 |
| VID/PID | `usbVendorId:"1D6B"`, `usbProductId:"0148"`. 이 빌드의 `devicesConfigs`에는 **이 장치 하나뿐** | 최상위 상수 [787] 보일러플레이트; `fn Tv@218884`에서 `"0x"+id` |
| 장치 설정 (C03P0BS0599 / P0B) | OS "RTOS", screenStatus "butt"(사각), screenRatio "1:2", **screenWidth 480, screenHeight 960**(세로형 패널), screenRotation 90, isWithScreen true, enableSyncStatus true, enableTransport true, enableScreenRotationAlgorithm false | jsc 원시 바이트 0x914b0~0x915a0 (Smi `60 c0 03 00 00`=480, `60 80 07 00 00`=960, `60 b4 00..`=90; 47=true, 48=false로 해석) **[코드/추정]** |
| 회전 보정 | screenRotation이 90/270이면 앱이 너비/높이를 바꿔 **960x480, "2:1"**로 사용 | `fn handleScreenDirection@23614 L10170`; 로그 `getDeviceResolution; screenWidth: 960 480` |
| HID 인터페이스 | usagePage 65280(0xFF00), usage 1, interface 0 | 로그 `hid device: {...}` |
| Report ID | **0** (`Ve.hidReportId = gn = 0`) | 최상위 @8304 `DefineNamedOwnProperty hidReportId ← slot[54]=0` (L2231, L1777) |
| 쓰기 형식 | `buf = [0x00] + payload`를 `device.write()`. hidapi가 1025바이트로 0 패딩하고 반환값은 항상 **1025** | `fn Si@18144 L8804`; 로그 `sendMsg request hid ret: 1025`, `buffer len: 55` |
| 읽기 | `'data'` 이벤트, 1024바이트 청크(앞에 `5A`, 뒤는 0 패딩). 스트림 재조립(`je.getMessagePacket/appendToCache`) | 로그 `handleShakeHandsResp; chunk: 5a01..00000`; `fn getMessagePacket@139985` |
| 보조 설정 `Ve` | maxFileBlockSize **1000**, maxMsglength **4096**, maxFileSize **33,554,432**, baudRate 115200(시리얼용) | jsc 원시 바이트 0x91240~0x91270 **[코드]** (1000은 21+1000+3=1024 리포트 크기와 정확히 맞음) |

**장치 계열별 경로**: 코드에는 HID / SerialPort / ADB 세 경로가 모두 있습니다(`fn tt/@142184 L42305`에서 `connType === "HID"` / `"SerialPort"` 분기). 기본 설정 `C05P01S0210`(1D6C:0119, Linux, 원형 1:1)은 시리얼/ADB 계열이고, ADB는 빌드 플래그로 꺼져 있습니다. **1D6B:0148은 HID 경로임이 확인**되었습니다(로그 `connType HID`, `sendMsg request hid ret`). **[확인]**

---

## 3. 메시지 계층

### 3.1 프레임 (링크 계층) **[확인]**

```
+------+-----------+---------------------------+----------+------+
| 0x5A | LEN (u16BE) | PAYLOAD (텍스트 메시지)      | CHECKSUM | 0x5A |
+------+-----------+---------------------------+----------+------+
         └──────────── 이 구간에 이스케이프 적용 ────────────┘
```

| 오프셋 | 크기 | 필드 | 설명 |
|---|---|---|---|
| 0 | 1 | SOF | `0x5A` (slot120 `pe`=90) |
| 1 | 2 | LEN | **이스케이프 전** 전체 길이 = payload + 5, big-endian (`qt`=ft(2)+ln(1)+2·1 = 5) |
| 3 | n | PAYLOAD | 3.2 참조 |
| 3+n | 1 | CHECKSUM | `(LEN 두 바이트 + PAYLOAD 모든 바이트의 합) & 0xFF` |
| 끝 | 1 | EOF | `0x5A` |

- **이스케이프(바이트 스터핑)**: LEN, PAYLOAD, CHECKSUM 구간에서 `0x5A` → `5B 01`, `0x5B` → `5B 02`로 바꿉니다(`Dt`=0x5B). 수신 측은 반대로 풉니다.
- 근거: `fn Vn@15347 L7901`(체크섬: len16+data 합 & 0xFF), `fn handleData@16876 L8479`, `fn cd@15526 L7965` + 콜백 `@15597 L8003`(이스케이프), `fn ud@15761` + `@15834 L8137`(디이스케이프), `fn rt@16314 L8315`, `rt.toBuffer@16556 L8356`(5A…5A), 최상위 상수 L2177-2199 (119:2, 120:90, 121:91, 122:1, 123:2, 124:1, 125:5, 126:6)
- 검증 예: conn 요청의 체크섬이 0x5B여서 `…0d0a 5b02 5a`로 끝나는 로그 프레임이 실제로 있습니다.

### 3.2 페이로드 (텍스트, HTTP 유사) **[확인]**

요청 (PC → 장치):
```
<METHOD> <cmd> 1\r\n
SeqNumber=<n>\r\n
Date=<epoch ms>\r\n
[ContentType=json\r\n
ContentLength=<바이트 수>\r\n]
\r\n
[<JSON 본문, 공백 없는 compact>]
```
- METHOD: `POST`, `STATE`(그 외 GET/DELETE 상수 존재). 버전 필드는 항상 `1`.
- SeqNumber: 연결마다 0부터 시작해 요청마다 +1(`fn ld@20124`, 10737418240에서 순환).
- 헤더 순서는 SeqNumber, Date, ContentType, ContentLength로 고정입니다(로그 3,885개와 일치).

응답 (장치 → PC):
```
1 200\r\n
AckNumber=<요청 Seq + 1>\r\n
[ContentType=json\r\nContentLength=<n>\r\n]
\r\n
[<JSON>]
```
- 근거: 클래스 `W`(요청, `fn W@18232 L8831`, getSeqNumber/getMessageBuffer), `Bt`(응답, getAckNumber/getRespCode/isSuccess), 팩토리 `de.create@20177 L9411`.

**예시 hex (실제 로그/인코더 결과)**
```
POST conn (Seq 0, Date 1789651447322):
5a0035 504f535420636f6e6e20310d0a5365714e756d6265723d300d0a446174653d313738393635313434373332320d0a0d0a 5b02 5a
        (체크섬 0x5B → 5B 02로 이스케이프)

STATE all {"heartbeat":1} (Seq 2):
5a0068535441544520616c6c20310d0a5365714e756d6265723d320d0a446174653d313738393635313434373438390d0a436f6e74656e74547970653d6a736f6e0d0a436f6e74656e744c656e6774683d31350d0a0d0a7b22686561727462656174223a317d 3d 5a

POST brightness {"value":50} (Seq 5):
5a006b504f5354206272696768746e65737320310d0a5365714e756d6265723d350d0a446174653d313738393635313434373332320d0a436f6e74656e74547970653d6a736f6e0d0a436f6e74656e744c656e6774683d31320d0a0d0a7b2276616c7565223a35307d 98 5a

응답 (AckNumber=2, 본문 없음):
5a001b 31203230300d0a41636b4e756d6265723d320d0a0d0a 2a 5a
```
HID로 보낼 때는 맨 앞에 Report ID `00`을 붙이면 되고, hidapi가 1025바이트까지 0으로 채웁니다.

---

## 4. 초기화 / 핸드셰이크 / keepalive

### 4.1 연결 순서 (실제 로그 2026-09-17 22:24:07, 코드와 일치) **[확인]**

| t(ms) | 방향 | 메시지 | 비고 |
|---|---|---|---|
| 0 | → | `POST conn` (본문 없음) | `fn @219516 L70784`: `once('data')` 등록 후 write. 타임아웃 값은 slot `hu`(값 **[미확인]**) |
| +100 | ← | `1 200`, `AckNumber=1`, JSON 217B | `{"OS":"RTOS","version":{"app":"V1.0.1","firmware":"V1.0.1","sdk":"V1.2.0","hardware":"V1.0"},"space":276,"brightness":21,"degree":270,"sn":"1234567890ABCDEF","osdState":0,"mode":0,"logo":0,"timeout":60,"bootFinish":1}` |
| +111 | → | `POST power {"event":"resume"}` | `fn dn@151550 L46018` ("唤醒设备") |
| +167 | → | `STATE all {"heartbeat":1}` | "同步状态" |
| +2,118 | → | `POST realtimeDisplay {"enable":true}` | 2000ms 지연 후 (`fn @152037`의 LdaSmi 2000) |
| 이후 | → | JPEG 프레임 스트림 | 5장 참조 |

- 장치 SN은 공장값 `1234567890ABCDEF`이고, USB 시리얼 `<USB 시리얼>`와 다릅니다. DarkFlash는 JSON의 `sn`을 키로 씁니다(store.json의 `devicesInfo` 키).
- 모든 요청에 대해 `1 200 / AckNumber` 응답이 옵니다. 응답은 AckNumber로 요청과 짝지어집니다(`ut.addRequestMessage`, MAX_REQUEST_COUNT).

### 4.2 Heartbeat **[확인]**
- `STATE all {"heartbeat":1}`, **4000ms 간격**(slot815 `Ps`=4000, `fn @207187 L66088` @302-422).
- 단, **실시간 전송이 꺼져 있을 때만** 보냅니다(`!isOpenedRealTimeTransport`). 실시간 전송 중에는 heartbeat 없이 프레임만 흐릅니다. 로그상 17일 하루 heartbeat 14회, 모두 4.0초 간격.
- 응답 본문은 비어 있습니다(`all status; resp body: []`).

### 4.3 기타 타이머 **[코드]**
| 타이머 | 값 | 근거 |
|---|---|---|
| 장치 틱 (실시간 켠 직후) | 1000/23 ms ≈ 43.5ms | `fn Zy@208713` (slot813=23) |
| 장치 틱 (realtimeDisplay 성공 후) | **1000/30 = 33.33ms** | `fn @208501 L66621` heap number 33.333333333333336 |
| 장치 틱 (기본) | 1000ms | `fn Bi@207852` |
| 제어 메시지 큐 처리 | 100ms (파일 전송 중에는 큐에 쌓임) | `fn wm@151134` |
| 오프스크린 렌더 창 frameRate | real 30 / osd 1 | `fn co@160990 L49392` |
| rotate 후 재동기화 | 500ms | `fn @201754` |

---

## 5. 화면 프레임 전송 (transType "real")

### 5.1 이미지 생성 **[코드 + 로그]**
1. 숨겨진 **오프스크린 BrowserWindow**(`graph.html`, `webPreferences.offscreen`)가 테마(위젯, 센서)를 렌더링합니다. `paint` 이벤트로 nativeImage를 받습니다(`fn openGraphWindow@159558`).
2. 창 크기는 `960/scaleFactor x 480/scaleFactor`입니다. 로그에서 배율 125%일 때 768x384, 150%일 때 640x320이었으므로 **물리 픽셀은 960x480**이 됩니다. **[추정]** 크롭 사각형은 `fn og@163392`.
3. `crop(rectangle)` 후 `hg(img, maxBytes=143360, minQ=50, startQ=85, step=5)`: 품질 85에서 시작해 140KB 이하가 될 때까지 5씩 낮춥니다(`fn @164537 L50776`, `fn hg@165094 L50992`). 로그: `Q: 85 │ S: 82.8KB`.
4. 결과를 `device.jpgData`에 저장합니다. **회전은 앱이 이미지에 적용하지 않고**(enableScreenRotationAlgorithm=false) 장치의 `rotate`/`degree`로 처리하는 것으로 보입니다. **[추정]**
- 포맷: **baseline JPEG**(Electron `nativeImage.toJPEG`). PNG, RGB565, raw 아님. (PNG 경로 `pngData`는 osd 모드용)

### 5.2 송신 스케줄 **[코드]**
`setInterval(틱)` → 장치마다 `fn @207187` 실행:
- `fileTransportStatus != "Idle"`(앞 프레임 전송 중)이면 건너뜀
- 전원 상태가 Suspend면 건너뜀
- `currentThemeId == 0`(MCU 내장 테마)이면 프레임을 보내지 않음
- `isOpenedRealTimeTransport && jpgData.length > 0`이면 `bm(sn, jpgData)` → `pm`으로 전송. **jpgData는 전송 후 비우지 않으므로 같은 이미지를 계속 재전송합니다.**
- 로그의 10초 간격 `[realTimeTransportFile]`, `[setImg]`는 **로그 스로틀**(`fn Gc@144400`, 10000ms) 때문이고 실제 전송 빈도가 아닙니다.

### 5.3 블록화 (파일 전송 공통) **[코드, 1024 크기 정합으로 강하게 뒷받침]**

`fn pm@144509 L43195`: `f = new qs(jpgBuffer, FileType.jpg)` → `while (f.hasNext()) await tt(device, f.valueWithTLV()); await bt(device)`

**HID 리포트 1개 = 블록 1개:**

| 오프셋(리포트 기준) | 크기 | 필드 | 값/설명 | 근거 |
|---|---|---|---|---|
| 0 | 1 | Report ID | `0x00` | `Si` |
| 1 | 1 | TLV Type | **`0x5C`** (slot112 `ad`=92) | `sd.toBuffer@13221 L7443` |
| 2 | 2 | TLV Length (u16BE) | 블록 헤더 21 + 데이터 길이 (최대 **1021 = 0x03FD**) | 〃 |
| 4 | 1 | File ID | `new Date().getSeconds()` (0~59), 파일마다 한 번 정함 | `qs@13587 L7538` @72-91 |
| 5 | 2 | BlockCount (u16BE) | `floor(size/1000) + (size%1000 ? 1 : 0)` | `qs` @260-333 |
| 7 | 2 | BlockIndex (u16BE) | 0부터 시작 | `od.create@13032 L7389`, `value@14500 L7716` |
| 9 | 1 | FileType | enum: unknown=0, **jpg=1**, png=2 | `fn @12917` (enum IIFE) |
| 10 | 15 | 예약 | 0 (헤더는 `Buffer.alloc(21)`, slot110 `wi`=21) | `od.create` |
| 25 | ≤1000 | 데이터 | JPEG 바이트를 순서대로 | `value()` |
| 나머지 | | 패딩 | 0 (hidapi) | |

- 마지막 블록 길이 = `size - 1000*(BlockCount-1)`, TLV Length도 그에 맞게 줄어듭니다.
- 블록에는 **시퀀스/체크섬 없음, `0x5A` 프레이밍과 이스케이프도 없음**(raw TLV). 요청 등록도 없어서(`tt` 3번째 인자 없음, `Uc=-1`) **블록별 ACK를 기다리지 않습니다**. 끝 표시는 BlockIndex == BlockCount-1뿐입니다.
- `realtime` 프레임에는 사전 `transport` 명령이 **없습니다**(pm에서 바로 블록 전송). 반면 장치 저장용 업로드(미디어/펌웨어)는 6장의 transport/transported로 감쌉니다.
- 예: 82,907바이트 JPEG → 83블록. 0번 블록 리포트 앞부분:
  `00 5C 03 FD | 2A 00 53 00 00 01 | 00×15 | FF D8 FF E0 …(1000B)`
  82번(마지막): `00 5C 03 A0 | 2A 00 53 00 52 01 | 00×15 | …(907B) | 0 패딩`
  (File ID 0x2A는 예시)

### 5.4 대역폭 / 부하
- 83KB 프레임 1장 = HID 쓰기 약 83회. DarkFlash는 최대 30fps 틱으로 **변경이 없어도** 계속 보냅니다(이론상 약 2.5MB/s, 전송 속도가 상한). → 대체 프로그램에서는 **센서 갱신 주기(예: 1~2초)마다 1장**이면 충분합니다.

---

## 6. 기타 명령 (모두 POST, 본문 JSON)

| cmd | 본문 | 의미 / 범위 | 호출 코드 | 확인 수준 |
|---|---|---|---|---|
| `conn` | 없음 | 핸드셰이크, 장치 속성 JSON 반환 | `@219516`, `Mv@220076` | [확인] |
| `power` | `{"event":"resume"}` / `{"event":"suspend"}` | 깨우기 / 화면 끄기(절전) | `@202740`, `@202967`, `dn@151550` | [확인] |
| (STATE) `all` | `{"heartbeat":1}` | heartbeat / 상태 동기화 | `@207187` | [확인] |
| `realtimeDisplay` | `{"enable":true/false}` | PC 스트리밍 모드 on/off | `@208197`, `@202571`, `@203079` | [확인] |
| `brightness` | `{"value":N}` | **0~100** (범위 밖이면 전송 안 함) | `xy@200826 L63379`, `@200910` | [확인] 로그 value 10~70 |
| `rotate` | `{"degree":N}` | **0~270** (0/90/180/270), 장치 측 회전 | `$y@201499 L63602`, `@201699` | [확인] 로그 90, 270 |
| `mode` | `{"value":N}` | 의미 [미확인]. 첫 실행 때 1 전송, 장치 응답 mode 0/1 | `Wy@203343`, `@203463` | 부분 |
| `timeout` | `{"value":N}` | IPC `setScreenTimeoutNotClose`/`Close`. 로그 값 0과 60(초 추정). 장치 기본 60 | `@203716`, `@203993` | 부분 (어느 쪽이 0인지 [미확인]) |
| `recovery` | `{"enable":true}` | 공장 초기화. 약 200ms 뒤 장치 재부팅(HID read 오류) | `Ly@201905` | [확인] 로그 |
| `reboot` | `{"enable":true}` | 재부팅 | `su@203232`, `@202368` | [코드] |
| `snSet` | `{"sn":"<시리얼>"}` | 장치 SN 쓰기 | `qy@202073` | [코드] |
| `upgrade` | `{"enable":true}` | 강제 업그레이드 모드(`forceUpgradeMode`) | `@204196` | [코드] |
| `presetThemeId` | `{"index":N}` | **MCU 내장 테마 선택** (IPC `themeCreator:mcuThemeIdSet`, store `mcuPresetThemeId`) | `Pg@168327 L52015`, 등록 `Tw@235933` | [코드] 인덱스 범위 [미확인] |
| `osdState` | `{"enable":bool}` | OSD 오버레이 on/off (osd 모드) | `Rg@173661 L53248`, `Ky@208808` | [코드] |
| `transport` | `{"type":"media"|"firmware","fileSize":N,"fileName":"…"}` | 장치 저장 업로드 시작(.zip이면 firmware) → 5.3과 같은 TLV 블록 → | `hm@146244 L44046`, `mm@146866` | [코드] |
| `transported` | `{"md5":"todo","fileName":"…"}` | 업로드 완료 | `@148263 L44978` | [코드] |
| `waterBlockScreen`, `waterBlockScreenId`, `sysinfoDisplay`, `displayInSleep`, `fanLCDSet`, `mediaDelete`, `config` | ? | 팩토리(`create@20177`)에만 있고 **호출 코드 없음** | | [미확인] |

- 장치가 먼저 보내는 상태 메시지(`handleStatusResponse@150490`: `fanLCD`, `turboPump`)는 다른 제품군용으로 보이며, 이 장치 로그에는 없습니다.
- **밝기/회전 읽기**: 별도 GET 명령 없이 `conn` 응답의 `brightness`, `degree`를 씁니다.
- **펌웨어 버전**: `conn` 응답 `version.firmware`.

---

## 7. 모드: transType "real" vs "osd", 내장 테마, 데이터 전용 여부

| 모드 | 동작 | PC 부하 |
|---|---|---|
| **real** (현재 설정) | PC가 전체 화면을 JPEG로 렌더링해 스트리밍 (`rn="real"` 초기값, slot826; 렌더 30fps) | 높음 |
| **osd** | 배경(img/video)은 `transport`로 장치에 업로드하고(`Ky@208808`: background.type PNG/JPG→img, 그 외 video), 위젯 오버레이는 PNG(`pngData`)로 만듦. 렌더 frameRate 1, `osdState` 명령 사용. **이 빌드의 틱 함수는 pngData를 계산만 하고 보내지 않음** → 사실상 미완성 경로로 보임 | 낮음(추정) |
| **내장 테마** (`currentThemeId == 0`, `presetThemeId{index}`) | 프레임 전송 없음, heartbeat만. 장치 펌웨어가 저장된 테마 표시 | 거의 없음 |

- **데이터 전용 모드**(센서 값만 보내고 장치가 그림): `sysinfoDisplay`, `fanLCDSet`, `displayInSleep` 명령이 팩토리에 있지만 **호출부와 본문 형식이 없어** 이 앱 버전에서는 쓰이지 않습니다. 센서 값을 계산하는 `fn Ig@170733`(CPU/GPU 온도, 사용률, 팬, 시간 등)의 결과는 렌더러(테마 창)로만 갑니다. **[코드]** → 펌웨어 지원 여부는 **[미확인]**.
- store.json의 `transStatus true`는 실시간 전송 활성 플래그, `transType "real"`은 위 real 모드입니다(`co@160990`).

---

## 8. 절전 / 잠금 / 모니터 wake 동작

| 이벤트 (Electron powerMonitor) | DarkFlash 동작 | 근거 |
|---|---|---|
| `lock-screen` | `realtimeDisplay{false}` → 약 400ms 뒤 `power{"event":"suspend"}` (설정 `lockWindowTurnOffScreen`) | `Jy@205982 L65576`, 로그 09-15 13:06:10 |
| `unlock-screen` | `power{"event":"resume"}` → 약 1s 뒤 `realtimeDisplay{true}` | 로그 09-15 13:06:17 |
| `suspend` (시스템 절전) | `realtimeDisplay{false}` → `power{suspend}` | 로그 09-14 01:46:14 |
| `resume` (시스템 복귀) | **아무것도 안 함** (핸들러 바이트코드 2바이트, `return undefined`) | `fn @206201` |
| HID `error`/`close` | 장치 삭제 후 재스캔하고 `conn`부터 다시 시작 | `@219818` ("hid device error" → deleteDevice) |

- 모니터 off/on 자체에 대한 처리는 없습니다. 로그를 보면 사용자의 WakeWatch가 모니터가 켜질 때마다 DarkFlash를 재시작했고, 재시작 뒤 `conn`부터 다시 핸드셰이크했습니다. HID `close` 이벤트는 recovery 명령 때만 기록되어, **절전 후 USB 재열거가 일어나는지는 [미확인]**입니다.
- 대체 프로그램 권장: resume/unlock/모니터 on 시 `power resume` → `realtimeDisplay true`를 보내고, write가 실패하거나 응답 타임아웃(예: 1초)이 나면 HID를 다시 열고 `conn`부터 재시작.

---

## 9. 미확인 사항과 확인 방법

| # | 미확인 | 영향 | 확인 방법 (이번에는 수행 안 함) |
|---|---|---|---|
| 1 | JPEG 실제 해상도(960x480 vs 480x960)와 회전 주체 | 화면 방향 | USBPcap + Wireshark로 DarkFlash 실행 중 캡처 → 블록 데이터를 이어 붙여 JPEG 헤더(SOF0)의 크기 확인. 또는 대체 프로그램에서 960x480 한 장 전송 테스트 |
| 2 | 블록 헤더 오프셋 10~24(15바이트)가 정말 0인지, File ID 규칙을 장치가 검사하는지 | 전송 성공 여부 | 위 캡처로 첫 리포트 hex 확인 (코드상 `Buffer.alloc(21)`에 6바이트만 씀) |
| 3 | 블록/프레임 단위로 장치가 응답(ACK, NAK)을 보내는지 | 흐름 제어 | 캡처의 IN 리포트 확인 (코드상 기다리지 않음) |
| 4 | 실시간 모드에서 프레임이 멈추면 `timeout`(60) 뒤 장치가 꺼지거나 로고로 가는지 → 최소 전송 주기 | 저전력 설계 | 대체 프로그램으로 프레임 1장 보낸 뒤 90초 관찰. 필요하면 30초마다 재전송 또는 `timeout{"value":0}` 시험 |
| 5 | `mode` 값의 의미, `timeout` 0과 60의 매핑 | 부가 기능 | DarkFlash UI 토글 중 캡처, 또는 conn 응답의 mode/timeout 변화 관찰 |
| 6 | `presetThemeId` index 범위, 내장 테마가 센서 값을 표시하는지 | 무부하 대안 | DarkFlash UI에서 MCU 테마 전환 중 캡처 |
| 7 | `sysinfoDisplay`/`fanLCDSet`/`displayInSleep` 본문 형식과 펌웨어 지원 | 데이터 전용 모드 | 이 앱 버전에서는 알아낼 수 없음. DarkFlash 신버전이나 다른 제품(Tryx Panorama 등, 같은 `panorama:` 문자열) 앱 분석 필요 |
| 8 | conn 응답 타임아웃(`hu`), 재시도 규칙 | 견고성 | 원시 jsc에서 slot 862 초기값 디코딩(최상위 @14xxx 부근) 또는 실험 |
| 9 | 최대 안정 전송 속도 | 애니메이션 | 대체 프로그램에서 fps 올려 가며 측정 |

---

## 10. 대체 프로그램 스켈레톤 (의사코드)

```python
import hid, json, time, io
VID, PID = 0x1D6B, 0x0148

def esc(b):  # 0x5A→5B01, 0x5B→5B02
    o = bytearray()
    for x in b:
        o += b'\x5b\x01' if x == 0x5A else b'\x5b\x02' if x == 0x5B else bytes([x])
    return bytes(o)

def frame(p):
    L = (len(p) + 5).to_bytes(2, 'big')
    cs = (sum(L) + sum(p)) & 0xFF
    return b'\x5a' + esc(L + p + bytes([cs])) + b'\x5a'

seq = 0
def req(dev, method, cmd, body=None):
    global seq
    s = f"{method} {cmd} 1\r\nSeqNumber={seq}\r\nDate={int(time.time()*1000)}\r\n"
    if body is not None:
        js = json.dumps(body, separators=(',', ':'))
        s += f"ContentType=json\r\nContentLength={len(js.encode())}\r\n\r\n{js}"
    else:
        s += "\r\n"
    seq += 1
    dev.write(b'\x00' + frame(s.encode()))   # hidapi가 1025B로 패딩
    return dev.read(1024, 1000)               # 5A..5A 응답 (1 200 / AckNumber)

def send_jpeg(dev, jpg):
    n = (len(jpg) + 999) // 1000
    fid = time.localtime().tm_sec
    for i in range(n):
        chunk = jpg[i*1000:(i+1)*1000]
        hdr = bytes([fid]) + n.to_bytes(2, 'big') + i.to_bytes(2, 'big') + b'\x01' + bytes(15)
        blk = hdr + chunk
        dev.write(b'\x00' + b'\x5c' + len(blk).to_bytes(2, 'big') + blk)

dev = hid.device(); dev.open(VID, PID)        # usagePage 0xFF00 인터페이스
props = req(dev, 'POST', 'conn')               # 응답 JSON: brightness/degree/version
req(dev, 'POST', 'power', {"event": "resume"})
req(dev, 'STATE', 'all', {"heartbeat": 1})
time.sleep(2)
req(dev, 'POST', 'realtimeDisplay', {"enable": True})
while True:
    jpg = render_960x480_jpeg_quality85()      # PIL 등, 140KB 이하
    send_jpeg(dev, jpg)
    time.sleep(1)                              # 변경 시에만/1~2초 간격 권장
```
(주의: 응답 파싱 시 5A 경계를 찾고, 디이스케이프한 뒤 체크섬을 검증하세요. 절전/잠금 처리는 8장 참고. 캡처로 #1~#4를 확인하기 전까지는 실험 코드입니다.)

---

## 11. 사용한 추출 파일 (스크래치패드)

기준 경로: `<분석 작업 폴더>\`

| 파일 | 내용 |
|---|---|
| `asar\` | app.asar 전체 추출 (`asar\out\main\index.jsc` = 분석 대상 바이트코드, `asar\package.json`) |
| `listing.txt` | asar 파일 목록 |
| `extract_asar.py` | asar 추출기 |
| `load_snap2.js` | jsc 역직렬화(실행 안 함) + 힙 스냅샷 |
| `main_code5.log` | V8 코드 로그(디스어셈블리 원본) |
| `main2.heapsnapshot` | 상수 풀 원본 |
| `build2.py` | 디스어셈블리 + 상수풀 병합기 |
| **`main_decomp.txt`** | **주석 달린 전체 바이트코드 목록 (본문 L번호 인용 대상)** |
| `deep.py`, `compact.py`, `cfg787.txt`, `devconfig_dump.txt` | 보일러플레이트(장치 설정) 덤프 |
| `main_strings.txt`, `preload_strings.txt` | jsc 문자열 |
| `logs\2026-09-16.log`, `logs\2026-09-17.log`, `logs\store.json`, `logs\settings.json`, `logs\wakewatch.log` | DarkFlash 로그/설정 읽기 전용 복사본 |
| `logmsgs.py`, `logmsgs_out.txt` | 로그 전체의 명령 종류와 빈도 (STATE all 3352, realtimeDisplay 206, power 174, conn 132, brightness 9, mode 4, recovery 3, rotate 3, timeout 2) |
| `verify_codec.py` | 프레임 인코더 재구현 + 로그 3,885개 프레임 대조 (**일치 3885 / 불일치 0**) |
| `electron\`, `electron.zip` | 역직렬화용 공식 Electron 25.9.7 (분석 도구, 삭제해도 됨) |
