"""격리된 단기 브라우저에서 공식 Kakao Roadview SDK를 렌더링합니다 (기존 pudo/roadview.py 기반).

정면 방향 설정은 기존 구현과 동일하게 유지합니다.
    pan = candidate.heading, tilt = 0, zoom = 0, view_mode = "curb-front"
카카오가 계산한 후보 지리 좌표 마커도 유지합니다. 단, 마커의 화면 y좌표는 원근 때문에
건물 앞 펜스·화단 위에 보일 수 있으므로 판독 위치로 쓰지 않고, 좌우(x) 방향 참고점으로만 씁니다.

이미지는 메모리에만 둡니다. 사용자 브라우저 세션·쿠키·임의 URL은 쓰지 않습니다.
"""
import io
import json
import math
import re
import time
from urllib.parse import quote

from .config import api_key

# 정면 판독 모드 상수 (기존 구현과 동일해야 함 — 테스트로 고정)
FRONT_TILT = 0
FRONT_ZOOM = 0
VIEW_MODE = "curb-front"
MAX_PAN_DELTA_DEG = 2
MAX_TILT_DEG = 2
MAX_ZOOM_DELTA = 0.1
VIEWPORT = (1280, 720)

# 진행이 멈춘 단계를 다시 시도하기 전 기다리는 시간과 재시도 횟수.
# 전체 시간 하나로 무조건 실패시키지 않고, 같은 단계에서 진행이 없을 때만 재시도합니다.
STALL_SECONDS = 25
MAX_RELOADS = 4

# 오류 단계 이름 (화면·로그에 그대로 표시)
STAGE_SDK_SCRIPT = "SDK 스크립트 로드"
STAGE_MAPS_LOAD = "Kakao maps.load 대기"
STAGE_PANO_ID = "파노라마 ID 조회"
STAGE_PANO_INIT = "파노라마 init 대기"
STAGE_FRONT = "정면 방향 설정"
STAGE_MARKER = "후보 마커 투영"
STAGE_TILES = "로드뷰 이미지 타일 수신"
STAGE_SCREENSHOT = "로드뷰 스크린샷"
STAGE_EMPTY = "로드뷰 이미지 검사"

_SECRET_PATTERNS = (
    re.compile(r"(appkey=)[^&\s\"'<>]+", re.I),
    re.compile(r"https?://dapi\.kakao\.com/\S*", re.I),
)


class RoadviewError(ValueError):
    def __init__(self, message, stage=None):
        message = redact(message)
        self.stage = stage
        super().__init__(f"[{stage}] {message}" if stage else message)


def redact(text):
    """오류 메시지에서 API 키가 포함된 URL·appkey 값을 제거합니다."""
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + "***") if m.groups() else "[SDK URL 숨김]", text)
    return text


# 카카오 로드뷰 화면 위 UI(이동 흰 화살표 _ar_*, 방향 글자 _at_*, 확대·나침반 버튼 _box_util_*)는 보도·연석·규제선을
# 가려 판독을 방해하므로 캡처 화면에서 숨깁니다. 사진 자체, 카카오 후보 마커, 저작권 표시는 그대로 둡니다.
HIDE_UI_CSS = ('#roadview [id^="_ar_"],#roadview [id^="_at_"],#roadview [id^="_box_util_"]'
               '{visibility:hidden!important}')


def viewer_html(key, candidate):
    lon, lat = candidate["coordinates"]
    heading = candidate["heading"]
    if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
               for n in (lon, lat, heading)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise RoadviewError("후보 좌표가 올바르지 않습니다.")
    data = json.dumps({"lon": lon, "lat": lat, "heading": heading})
    sdk = "https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey=" + quote(key, safe="")
    return """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#roadview{margin:0;width:100%;height:100%;overflow:hidden}
__HIDE_UI_CSS__</style></head>
<body><div id="roadview"></div><script>
window.captureStartedAt=Date.now();
window.captureState={status:'loading',stage:'__ST_SDK__'};
function setStage(s){window.captureState.stage=s;window.captureStageAt=Date.now()}
window.captureStageAt=Date.now();
window.addEventListener('error',()=>{if(window.captureState.status==='loading')window.captureState={status:'error',stage:window.captureState.stage,error:'브라우저 스크립트 오류로 로드뷰 초기화에 실패했습니다.'}});
const c=__CANDIDATE__;
function startRoadview(){
  setStage('__ST_MAPS__');
  kakao.maps.load(()=>{
    setStage('__ST_PANO_ID__');
    const position=new kakao.maps.LatLng(c.lat,c.lon);
    const rv=new kakao.maps.Roadview(document.getElementById('roadview'));
    kakao.maps.event.addListener(rv,'init',()=>{
      try {
        setStage('__ST_FRONT__');
        const camera=rv.getPosition();
        const projected=rv.getProjection().viewpointFromCoords(position,0);
        // candidate.heading is the normal from the road centerline toward this
        // curb. Use it for every candidate so the curb/building frontage is
        // viewed straight-on instead of aiming diagonally from the panorama
        // camera at the candidate point.
        const frontPan=c.heading;
        const frontTilt=0;
        const frontZoom=0;
        // Kakao geographic marker of the candidate. Its screen x shows on which
        // side the candidate lies; its y is NOT the curb (perspective).
        const targetViewpoint=new kakao.maps.Viewpoint(projected.pan,projected.tilt,0,rv.getPanoId());
        const marker=new kakao.maps.Marker({position:targetViewpoint,map:rv});
        const anchor=new kakao.maps.CustomOverlay({position:targetViewpoint,map:rv,
          xAnchor:0.5,yAnchor:0.5,
          content:'<div id="vlm-target-anchor" style="width:16px;height:16px;border:3px solid #e11;background:#fff8;border-radius:50%;box-sizing:border-box"></div>'});
        // 정면(pan=heading, tilt=0, zoom=0)만 사용. 후보 마커는 화면에 보이면 참고로 기록하고,
        // 보이지 않아도 실패로 처리하지 않습니다 (판독 기준은 화면 가운데).
        let checks=0, completed=false;
        const markerPixel=()=>{
          const e=document.getElementById('vlm-target-anchor');
          if(!e)return null;
          const r=e.getBoundingClientRect();
          if(!(r.width>0&&r.height>0&&r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight))return null;
          return {x:r.x+r.width/2,y:r.y+r.height/2,width:innerWidth,height:innerHeight};
        };
        const finish=(point)=>{
          completed=true;
          window.captureState={status:'ready',pano_id:String(rv.getPanoId()),
            stage:'정면 로드뷰 준비 완료',
            camera:[camera.getLng(),camera.getLat()],query_heading:c.heading,
            viewpoint:rv.getViewpoint(),target_bearing:projected.pan,
            view_mode:'curb-front',target_marker:!!point,target_pixel:point};
        };
        const finishWhenProjected=()=>{
          if(completed)return;
          setStage('__ST_MARKER__');
          const v=rv.getViewpoint();
          const d=Math.abs(((v.pan-frontPan)%360+540)%360-180);
          const point=markerPixel();
          if(point&&d<=2){finish(point);return}
          if(++checks<60){setTimeout(finishWhenProjected,50);return}
          if(d<=2)finish(null);   // 정면은 맞췄지만 후보 마커가 화면 밖 → 마커 없이 진행
          else window.captureState={status:'error',stage:'__ST_FRONT__',error:'후보 도로변 정면 방향으로 로드뷰를 돌리지 못했습니다.'};
        };
        kakao.maps.event.addListener(rv,'viewpoint_changed',()=>{if(!completed&&markerPixel()&&checks>0)finishWhenProjected()});
        rv.setViewpoint({pan:frontPan,tilt:frontTilt,zoom:frontZoom});
        setTimeout(finishWhenProjected,0);
      } catch(e){window.captureState={status:'error',stage:'__ST_FRONT__',error:'후보 쪽 로드뷰 방향을 설정하지 못했습니다.'}}
    });
    new kakao.maps.RoadviewClient().getNearestPanoId(position,30,pano=>{
      if(!pano){window.captureState={status:'error',stage:'__ST_PANO_ID__',error:'파노라마 ID 조회 실패: 후보 반경 30m 안에 로드뷰가 없습니다.'};return}
      setStage('__ST_PANO_INIT__');
      rv.setPanoId(pano,position);
    });
  });
}
</script><script src="__SDK__" onload="startRoadview()" onerror="window.captureState={status:'error',stage:'__ST_SDK__',error:'SDK 스크립트 로드 실패: 키·네트워크·Web 도메인 등록을 확인하세요.'}"></script></body></html>""" \
        .replace("__CANDIDATE__", data).replace("__SDK__", sdk.replace("&", "&amp;")) \
        .replace("__HIDE_UI_CSS__", HIDE_UI_CSS) \
        .replace("__ST_SDK__", STAGE_SDK_SCRIPT).replace("__ST_MAPS__", STAGE_MAPS_LOAD) \
        .replace("__ST_PANO_ID__", STAGE_PANO_ID).replace("__ST_PANO_INIT__", STAGE_PANO_INIT) \
        .replace("__ST_FRONT__", STAGE_FRONT).replace("__ST_MARKER__", STAGE_MARKER)


def _finite(n):
    return isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)


def _angle(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def capture_distance_m(camera, candidate):
    lon, lat = candidate["coordinates"]
    dx = (camera[0] - lon) * 111320 * math.cos(math.radians(lat))
    dy = (camera[1] - lat) * 111320
    return math.hypot(dx, dy)


def camera_offset(camera, candidate):
    """촬영 위치 → 후보: 도로를 따라 떨어진 거리(along_road_m)와 도로변 쪽 거리(toward_curb_m).

    화면 가운데는 카메라 바로 앞 도로변이므로, along_road_m가 크면 가운데 판독 지점이 후보에서 그만큼 옆입니다.
    """
    lon, lat = candidate["coordinates"]
    dx = (lon - camera[0]) * 111320 * math.cos(math.radians(lat))
    dy = (lat - camera[1]) * 111320
    h = math.radians(candidate["heading"])
    toward = dx * math.sin(h) + dy * math.cos(h)
    along = dx * math.cos(h) - dy * math.sin(h)
    return {"along_road_m": round(along, 2), "toward_curb_m": round(toward, 2)}


def validate_capture(metadata, candidate):
    """기존 정면 판독 검증: pan−heading ≤ 2°, |tilt| ≤ 2°, |zoom| ≤ 0.1, view_mode=curb-front."""
    if not isinstance(metadata, dict) or metadata.get("status") != "ready":
        raise RoadviewError("로드뷰가 준비되지 않았습니다.")
    camera = metadata.get("camera")
    if not isinstance(camera, list) or len(camera) != 2 or not all(_finite(n) for n in camera):
        raise RoadviewError("로드뷰 촬영 좌표를 확인할 수 없습니다.")
    if not (-180 <= camera[0] <= 180 and -90 <= camera[1] <= 90):
        raise RoadviewError("로드뷰 촬영 좌표가 올바르지 않습니다.")
    distance = capture_distance_m(camera, candidate)
    if distance > 30 or distance < 1:
        raise RoadviewError("촬영 지점이 후보에서 너무 멀거나 가까워 대상 도로변을 특정할 수 없습니다.")
    v = metadata.get("viewpoint")
    if not isinstance(v, dict) or not all(_finite(v.get(k)) for k in ("pan", "tilt", "zoom")):
        raise RoadviewError("로드뷰 방향을 확인할 수 없습니다.")
    if (_angle(v["pan"], candidate["heading"]) > MAX_PAN_DELTA_DEG or abs(v["tilt"] - FRONT_TILT) > MAX_TILT_DEG
            or abs(v["zoom"] - FRONT_ZOOM) > MAX_ZOOM_DELTA):
        raise RoadviewError("후보 도로변의 정면 방향·수평 각도·기본 확대 상태로 로드뷰를 맞추지 못했습니다.")
    if metadata.get("view_mode") != VIEW_MODE:
        raise RoadviewError("로드뷰가 후보 도로변 정면 판독 모드인지 확인할 수 없습니다.")
    if not isinstance(metadata.get("pano_id"), str) or not metadata["pano_id"]:
        raise RoadviewError("로드뷰 사진 ID를 확인할 수 없습니다.")
    return {**metadata, "distance_m": round(distance, 2), "camera_offset": camera_offset(camera, candidate)}


def validate_target_pixel(point, size=VIEWPORT):
    """카카오 후보 마커의 화면 위치 (참고 표시용). 화면에 없으면 None — 판독을 막지 않습니다."""
    if not isinstance(point, dict) or not all(_finite(point.get(k)) for k in ("x", "y", "width", "height")):
        return None
    if not (0 <= point["x"] < point["width"] and 0 <= point["y"] < point["height"]):
        return None
    return point


ROADVIEW_IMAGE_PATTERN = "https://map.kakaocdn.net/map_roadview/**"


def add_cors_header(route):
    """카카오 로드뷰 사진 응답에 CORS 허용 헤더만 추가 (내용은 그대로)."""
    try:
        response = route.fetch()
    except Exception:
        route.continue_()
        return
    headers = {**response.headers, "access-control-allow-origin": "*"}
    route.fulfill(response=response, headers=headers)


def check_not_blank(png_bytes):
    from PIL import Image, ImageStat
    with Image.open(io.BytesIO(png_bytes)) as screenshot:
        if max(ImageStat.Stat(screenshot.convert("RGB")).stddev) < 5:
            raise RoadviewError("로드뷰 이미지가 비어 있습니다. 판독을 중단했습니다.", STAGE_EMPTY)


class KakaoRoadview:
    def __init__(self, key=None, log=print):
        self.key = key
        self.log = log

    def _log(self, candidate, message):
        self.log(f"로드뷰 진단 {candidate.get('id', '?')}: {redact(message)}", flush=True)

    def __call__(self, candidate):
        key = self.key or api_key("kakao")
        if not key:
            raise RoadviewError("카카오 JavaScript 키가 필요합니다. .env에 KAKAO_MAPS_JS_KEY를 설정하세요.", "설정")
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        except ImportError:
            raise RoadviewError("playwright를 설치하고 python -m playwright install chromium을 실행하세요.", "설정") from None
        started = time.monotonic()
        stage = "브라우저 시작"
        network_idle = True
        try:
            with sync_playwright() as p:
                stage = "Chromium 실행"
                browser = p.chromium.launch(headless=True)
                try:
                    width, height = VIEWPORT
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                    page = context.new_page()
                    page.set_default_timeout(35000)
                    html = viewer_html(key, candidate)
                    failed_images = []
                    page.on("response", lambda response: failed_images.append(response.status)
                            if response.status >= 400 and response.request.resource_type == "image" else None)
                    # Kakao Web 도메인에 등록된 주소. 실제 서버가 아니라 route로 메모리 HTML을 응답합니다.
                    url = "http://localhost:8000/__vlm_roadview__"
                    page.route(url, lambda route: route.fulfill(body=html, content_type="text/html"))
                    # 카카오 CDN이 일부 로드뷰 사진(예: 2026년 정육면체 형식)에 localhost 출처 CORS 허용 헤더를
                    # 붙이지 않아 SDK가 사진을 못 읽고 init이 오지 않음. 이 격리 브라우저 안에서만 사진 요청을
                    # 대신 받아 허용 헤더를 붙여 넘깁니다 (사진 내용은 바꾸지 않음).
                    page.route(ROADVIEW_IMAGE_PATTERN, add_cors_header)
                    blocked_images = []
                    page.on("requestfailed", lambda request: blocked_images.append(request.failure)
                            if request.resource_type == "image" and "kakaocdn.net/map_roadview" in request.url else None)
                    self._log(candidate, "시작")
                    metadata = None
                    for attempt in range(1, MAX_RELOADS + 1):
                        failed_images.clear()
                        blocked_images.clear()
                        stage = "로드뷰 페이지 열기"
                        page.goto(url, wait_until="domcontentloaded", timeout=0)
                        stage = STAGE_SDK_SCRIPT
                        # 같은 단계에서 STALL_SECONDS 동안 진행이 없을 때만 재시도합니다.
                        page.wait_for_function(
                            "window.captureState && (window.captureState.status !== 'loading' || "
                            f"Date.now() - window.captureStageAt >= {STALL_SECONDS * 1000})",
                            timeout=0,
                        )
                        metadata = page.evaluate("window.captureState")
                        stage = metadata.get("stage") or stage
                        if metadata.get("status") != "loading":
                            break
                        if blocked_images:
                            raise RoadviewError(
                                f"로드뷰 사진 파일 {len(blocked_images)}개를 브라우저가 받지 못했습니다 "
                                f"({blocked_images[0]}). 카카오 CDN 응답을 확인하세요.", stage)
                        self._log(candidate, f"'{stage}' 단계에서 {STALL_SECONDS}초 동안 진행 없음, 다시 시도 {attempt}/{MAX_RELOADS}")
                        page.wait_for_timeout(1000)
                    if metadata.get("status") == "loading":
                        raise RoadviewError(
                            f"{MAX_RELOADS}회 다시 시도했지만 이 단계에서 진행되지 않았습니다 "
                            f"(경과 {time.monotonic() - started:.0f}초).", stage)
                    if metadata.get("status") == "error":
                        raise RoadviewError(metadata.get("error", "로드뷰 연결 실패"), metadata.get("stage") or stage)
                    stage = STAGE_FRONT
                    metadata = validate_capture(metadata, candidate)
                    try:
                        stage = STAGE_TILES
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except BrowserTimeout:
                        network_idle = False   # 배경 타일 요청이 계속돼도 정상 캡처는 버리지 않음
                    page.wait_for_timeout(1500)
                    if failed_images:
                        statuses = ", ".join(map(str, sorted(set(failed_images))))
                        raise RoadviewError(f"로드뷰 이미지 일부가 HTTP {statuses}로 실패해 판독을 중단했습니다.", STAGE_TILES)
                    stage = STAGE_MARKER
                    point = page.evaluate("""() => {
                        const e=document.getElementById('vlm-target-anchor');
                        if(!e) return null;
                        const r=e.getBoundingClientRect();
                        if(r.width===0 || r.height===0 || getComputedStyle(e).visibility==='hidden') return null;
                        return {x:r.x+r.width/2,y:r.y+r.height/2,width:innerWidth,height:innerHeight};
                    }""")
                    metadata["target_pixel"] = validate_target_pixel(point)
                    metadata["network_idle"] = network_idle
                    stage = STAGE_SCREENSHOT
                    image = page.locator("#roadview").screenshot(type="png", timeout=15000)
                finally:
                    browser.close()
        except BrowserTimeout:
            raise RoadviewError(f"시간 초과 ({time.monotonic() - started:.1f}초).", stage) from None
        except RoadviewError:
            raise
        except Exception as error:
            # 브라우저 오류에는 키가 들어간 SDK URL이 있을 수 있으므로 종류만 남깁니다.
            raise RoadviewError(f"브라우저 실행 실패 ({type(error).__name__}). Chromium 설치와 시스템 라이브러리를 확인하세요.",
                                stage) from None
        check_not_blank(image)
        metadata["image_encoding"] = "png"
        metadata["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        self._log(candidate, f"성공 {metadata['elapsed_ms']}ms, 정면 {metadata['viewpoint']['pan']:.1f}°, "
                             f"후보 마커 {'화면 안' if metadata.get('target_pixel') else '화면 밖(참고 표시 없음)'}, network_idle={network_idle}")
        return image, metadata
