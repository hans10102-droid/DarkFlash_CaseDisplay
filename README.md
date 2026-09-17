# CaseDisplay

DarkFlash 케이스 화면(LCD, USB HID `1D6B:0148`, 960×480)을 DarkFlash 앱 없이 구동하는 가벼운 드라이버와 웹 설정 화면입니다.

| | DarkFlash 앱 | CaseDisplay |
|---|---|---|
| 프로세스 | Electron 5개 | 1개 |
| 메모리 | 약 570MB | 약 40MB |
| CPU (전체 대비) | 약 0.9% | 약 0.03% |
| 화면 전송 | 초당 30회 반복 | 값이 바뀔 때만 |

## 기능

- 위젯 6종: 글자, 센서 값, 막대(연속/칸 나눔, 둥근 모서리), 원형 링, 반원 속도계, 기록 그래프
  - 모든 위젯에 반투명 카드 배경, 값 구간별 글자색(예: 80℃ 이상 빨강)
- DarkFlash 테마(`.thm`에서 꺼낸 JSON)는 불러올 때 자동 변환
- 센서: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 웹 서버(`http://localhost:8085/data.json`)
- 잠금 화면 또는 모니터 절전 시 케이스 화면 끄기, 해제 시 켜기
- 표시값이 바뀔 때만 렌더링·전송, 테마 오류가 나도 연결과 마지막 화면 유지
- USB 오류 시 자동 재연결, 설정·테마 파일 변경 즉시 반영, 코드 변경 시 자동 재시작
- 설정 화면 `http://127.0.0.1:8765/`
  - 위젯 드래그 배치, 추가/삭제, 글꼴·색·센서 선택
  - 배경 이미지 2:1 자르기 (서버가 원본에서 잘라 Lanczos로 960×480 축소, 쓰지 않는 배경은 하루 뒤 정리)
  - 되돌리기/다시 실행 (Ctrl+Z / Ctrl+Y)
  - 설정 기록(이름 붙인 기록 + 저장할 때마다 자동 기록)
  - 밝기, 회전, 갱신 주기

## 구성

| 파일 | 역할 |
|---|---|
| `casedisplay.py` | 메인 루프 (연결, 잠금·절전 처리, 전송, 재연결) |
| `lcd_protocol.py` | 케이스 화면 HID 프로토콜 |
| `theme_render.py` | 테마 렌더러, LHM 센서 읽기 |
| `fonts.py` | 설치된 글꼴 찾기 |
| `webui.py`, `editor.html` | 설정 화면 |
| `config.json` | 밝기, 회전, 갱신 주기 등 |
| `theme/theme.json` | 테마 (배경 이미지는 저장소에 포함하지 않음) |
| `examples/demo_theme.json` | 위젯 6종을 모두 쓴 예시 테마 |
| `docs/PROTOCOL.md` | 프로토콜 분석 문서 |
| `tools/df_screen.py` | 프로토콜 시험용 단독 스크립트 |

## 테마 형식 (v2)

좌표는 케이스 화면 기준 960×480 픽셀입니다. 위젯은 목록 순서대로 그려집니다(뒤쪽이 위).

```json
{
 "version": 2,
 "background": {"path": "background.png"},
 "widgets": [
  {"id": "r1", "type": "ring", "x": 30, "y": 40, "size": 150, "thickness": 14,
   "source": "CPU Temperature", "min": 20, "max": 95,
   "colorRanges": [{"from": 80, "color": "rgba(255,90,90,1)"}],
   "card": {"enabled": true, "color": "rgba(0,0,0,0.35)", "radius": 16, "padding": 8}}
 ]
}
```

| type | 주요 속성 |
|---|---|
| `text` | `content`, `font{family,size,bold}`, `color` |
| `value` | `source`, `decimals`, `unit`, `font`, `color`, `colorRanges` |
| `bar` | `source`, `w`, `h`, `min`, `max`, `bg`, `colorStart`, `colorEnd`, `segments`, `gap`, `radius` |
| `ring` | `source`, `size`, `thickness`, `startAngle`, `sweep`, `roundCap`, `label`, `labelFont`, `labelColor` |
| `gauge` | `source`, `size`, `thickness`, `ticks`, `tickColor`, `needleColor`, `label` |
| `graph` | `source`, `w`, `h`, `seconds`, `min`/`max`(비우면 자동), `style`(`area`/`line`), `lineColor`, `fillColor`, `grid` |

`source`는 이름(`CPU Temperature`, `GPU Usage`, `Memory Usage`, `Time` 등)으로 지정하며 LHM 센서 ID가 바뀌어도 이름 규칙으로 다시 찾습니다. LHM 센서를 직접 고르면 `lhm:/amdcpu/0/load/0` 형식이 됩니다.

## 설치

```powershell
pip install -r requirements.txt
```

1. LibreHardwareMonitor를 실행하고 **Options → Remote Web Server → Run**(포트 8085)을 켭니다.
2. `theme/` 폴더에 배경 이미지를 넣거나, 설정 화면에서 **배경 이미지 변경**으로 올립니다.
3. 실행합니다.

```powershell
python casedisplay.py --console
```

작업 관리자에 `CaseDisplay`로 보이게 하려면 `pythonw.exe`를 `CaseDisplay.exe`로 복사하고, 같은 폴더에 `python313.dll`, `python3.dll`, `vcruntime140*.dll`과 아래 `pyvenv.cfg`를 둡니다.

```
home = C:\...\Python313
include-system-site-packages = true
```

로그온 시 자동 실행은 작업 스케줄러에 `CaseDisplay.exe casedisplay.py`를 등록합니다.

## 주의

- **DarkFlash 앱과 동시에 실행하지 마세요.** 두 프로그램이 같은 장치에 동시에 쓰면 케이스 화면 펌웨어가 멈추고, 전원을 완전히 차단해야 복구됩니다.
- 로그: `C:\ProgramData\CaseDisplay.log`
