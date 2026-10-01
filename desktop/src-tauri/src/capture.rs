//! Generation-private manual screen capture resources and per-monitor overlay windows.

use std::{
    collections::{HashMap, VecDeque},
    fs::{self, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};

use crate::observer_sources::{self, ForegroundTarget, ObserverSources};
use image::{codecs::jpeg::JpegEncoder, imageops::FilterType, ExtendedColorType, ImageEncoder};
use serde::{Deserialize, Serialize};
use tauri::{
    webview::{Color, WebviewBuilder},
    window::WindowBuilder,
    AppHandle, LogicalSize, Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow,
};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use uuid::Uuid;
use xcap::Monitor;

pub const SCREEN_OBSERVATION_SELF: &str = "SCREEN_OBSERVATION_SELF";
pub const SCREEN_OBSERVATION_PRIVACY_BLOCKED: &str = "SCREEN_OBSERVATION_PRIVACY_BLOCKED";
const MAX_PRIVACY_ENTRIES: usize = 64;
const MAX_PRIVACY_ENTRY_CHARS: usize = 128;
pub const ATTACHED_EVENT: &str = "sakura://screen-attachment";
pub const CANCELLED_EVENT: &str = "sakura://screen-capture-cancelled";
pub const ERROR_EVENT: &str = "sakura://screen-capture-error";
const RESOURCE_DIRECTORY: &str = "sakura-runtime-v2-screen-resources";
const RESOURCE_TTL: Duration = Duration::from_secs(120);
const MAX_CAPTURE_BYTES: usize = 24 * 1024 * 1024;
const MAX_CAPTURE_PIXELS: u64 = 32_000_000;
const MAX_SCREEN_AWARENESS_BATCH_BYTES: usize = 64 * 1024 * 1024;
const MAX_CAPTURE_EDGE: u32 = 1280;
const MIN_SELECTION_LOGICAL_PX: f64 = 8.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PhysicalRect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

#[derive(Clone, Debug)]
pub struct CaptureMonitor {
    pub id: u32,
    pub name: String,
    pub bounds: PhysicalRect,
    pub primary: bool,
}

#[derive(Clone, Debug)]
struct CaptureSession {
    id: String,
    generation_id: String,
    windows: HashMap<String, u32>,
}

#[derive(Clone, Debug)]
struct CaptureResource {
    path: PathBuf,
    generation_id: String,
    created_at: Instant,
}

#[derive(Clone, Debug)]
struct ScreenAwarenessFrame {
    bytes: Vec<u8>,
    width: u32,
    height: u32,
    captured_at: String,
    screen_name: String,
}

#[derive(Clone, Debug)]
pub struct CaptureClaim {
    pub generation_id: String,
    pub monitor_id: u32,
    pub window_labels: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenResourceDescriptor {
    pub generation_id: String,
    pub resource_token: String,
    pub mime_type: &'static str,
    pub width: u32,
    pub height: u32,
    pub byte_length: usize,
    pub captured_at: String,
    pub screen_name: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenAttachmentPublication {
    pub attachment_id: String,
    pub item_id: String,
    pub width: u32,
    pub height: u32,
    pub count: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenAttachmentItemRemovePublication {
    pub accepted: bool,
    pub attachment_id: String,
    pub item_id: String,
    pub count: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenAwarenessCapturePublication {
    pub count: usize,
    #[serde(skip_serializing)]
    pub dropped_count: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScreenAwarenessAttachmentPublication {
    pub attachment_id: String,
    pub count: usize,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CaptureSelectionRequest {
    pub session_id: String,
    pub monitor_id: u32,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CaptureCancelRequest {
    pub session_id: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AttachmentReleaseRequest {
    pub attachment_id: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AttachmentItemRemoveRequest {
    pub attachment_id: String,
    pub item_id: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScreenAwarenessCaptureRequest {
    pub resolution: String,
    pub batch_limit: usize,
    pub scope: Option<String>,
    pub capture_ticket: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScreenAwarenessOfferRequest {
    pub scope: String,
    pub capture_ticket: String,
}

impl ScreenAwarenessCaptureRequest {
    pub fn offer(&self) -> Result<Option<ScreenAwarenessOfferRequest>, String> {
        match (&self.scope, &self.capture_ticket) {
            (None, None) => Ok(None),
            (Some(scope), Some(ticket))
                if self.batch_limit == 1 && self.resolution == "fullscreen" =>
            {
                Ok(Some(ScreenAwarenessOfferRequest {
                    scope: scope.clone(),
                    capture_ticket: ticket.clone(),
                }))
            }
            _ => Err("SCREEN_AWARENESS_SETTINGS_INVALID".into()),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ForegroundOffer {
    generation_id: String,
    ticket: String,
    pub target: ForegroundTarget,
    pub visible_text: String,
    created_at: Instant,
}

pub struct CaptureManager {
    base_root: PathBuf,
    available: bool,
    state: Mutex<CaptureState>,
    focus_revision: AtomicU64,
    pub observer_sources: ObserverSources,
}

pub fn valid_attachment_id(value: &str) -> bool {
    value.strip_prefix("screen-").is_some_and(|token| {
        token.len() == 32
            && token
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

pub fn valid_attachment_item_id(value: &str) -> bool {
    value.strip_prefix("shot-").is_some_and(|token| {
        token.len() == 32
            && token
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

#[derive(Default)]
struct CaptureState {
    active: Option<CaptureSession>,
    resources: HashMap<String, CaptureResource>,
    active_generation: Option<String>,
    awareness_frames: VecDeque<ScreenAwarenessFrame>,
    foreground_only: bool,
    foreground_offer: Option<ForegroundOffer>,
}

/// Casefolded foreground-window blocklist owned by the Core screen-awareness settings.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ScreenPrivacy {
    pub blocked_processes: Vec<String>,
    pub blocked_title_keywords: Vec<String>,
}

impl ScreenPrivacy {
    pub fn from_core_payload(value: &serde_json::Value) -> Result<Self, String> {
        let invalid = || "SCREEN_PRIVACY_RESPONSE_INVALID".to_string();
        let object = value
            .as_object()
            .filter(|object| {
                object.len() == 3
                    && object
                        .get("schemaVersion")
                        .and_then(serde_json::Value::as_u64)
                        == Some(1)
            })
            .ok_or_else(invalid)?;
        let list = |key: &str| -> Result<Vec<String>, String> {
            let values = object
                .get(key)
                .and_then(serde_json::Value::as_array)
                .filter(|values| values.len() <= MAX_PRIVACY_ENTRIES)
                .ok_or_else(invalid)?;
            values
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|text| text.chars().count() <= MAX_PRIVACY_ENTRY_CHARS)
                        .map(|text| text.trim().to_lowercase())
                        .ok_or_else(invalid)
                })
                .filter(|entry| entry.as_ref().map_or(true, |text| !text.is_empty()))
                .collect()
        };
        Ok(Self {
            blocked_processes: list("blockedProcesses")?,
            blocked_title_keywords: list("blockedTitleKeywords")?,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ForegroundWindow {
    pub hwnd: u64,
    pub process_id: u32,
    pub process_name: String,
    pub title: String,
}

/// Why a scheduled observation must not look at the screen right now.
pub fn screen_observation_block(
    privacy: &ScreenPrivacy,
    foreground: Option<&ForegroundWindow>,
    own_process_id: u32,
) -> Option<&'static str> {
    let foreground = foreground?;
    if foreground.process_id == own_process_id {
        return Some(SCREEN_OBSERVATION_SELF);
    }
    let process = foreground.process_name.to_lowercase();
    if !process.is_empty()
        && privacy
            .blocked_processes
            .iter()
            .any(|blocked| *blocked == process)
    {
        return Some(SCREEN_OBSERVATION_PRIVACY_BLOCKED);
    }
    let title = foreground.title.to_lowercase();
    if !title.is_empty()
        && privacy
            .blocked_title_keywords
            .iter()
            .any(|keyword| title.contains(keyword.as_str()))
    {
        return Some(SCREEN_OBSERVATION_PRIVACY_BLOCKED);
    }
    None
}

/// Snapshot sent to Core. The title stays on this hop so same-app renames do not
/// reset dwell; diagnostic output must not copy it.
pub fn focus_advance_snapshot(
    foreground: Option<&ForegroundWindow>,
    own_process_id: u32,
) -> Option<serde_json::Value> {
    let foreground = foreground?;
    Some(serde_json::json!({
        "hwnd": foreground.hwnd,
        "pid": foreground.process_id,
        "process": foreground.process_name,
        "title": foreground.title,
        "ownProcess": foreground.process_id == own_process_id,
    }))
}

#[cfg(windows)]
pub fn foreground_window() -> Option<ForegroundWindow> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
    };

    let hwnd = unsafe { GetForegroundWindow() };
    if hwnd.0.is_null() {
        return None;
    }
    let mut process_id = 0u32;
    unsafe { GetWindowThreadProcessId(hwnd, Some(&mut process_id)) };
    let mut title = [0u16; 512];
    let title_len = unsafe { GetWindowTextW(hwnd, &mut title) }.max(0) as usize;
    let mut process_name = String::new();
    if let Ok(process) =
        unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, process_id) }
    {
        let mut path = [0u16; 1024];
        let mut size = path.len() as u32;
        if unsafe {
            QueryFullProcessImageNameW(
                process,
                PROCESS_NAME_WIN32,
                PWSTR(path.as_mut_ptr()),
                &mut size,
            )
        }
        .is_ok()
        {
            let full = String::from_utf16_lossy(&path[..size as usize]);
            process_name = full
                .rsplit(['\\', '/'])
                .next()
                .unwrap_or_default()
                .to_string();
        }
        let _ = unsafe { CloseHandle(process) };
    }
    Some(ForegroundWindow {
        hwnd: hwnd.0 as usize as u64,
        process_id,
        process_name,
        title: String::from_utf16_lossy(&title[..title_len.min(title.len())]),
    })
}

#[cfg(not(windows))]
pub fn foreground_window() -> Option<ForegroundWindow> {
    None
}

impl CaptureManager {
    pub fn new() -> Self {
        let base_root = std::env::temp_dir().join(RESOURCE_DIRECTORY);
        Self::with_base(base_root.clone()).unwrap_or(Self {
            base_root,
            available: false,
            state: Mutex::new(CaptureState::default()),
            focus_revision: AtomicU64::new(0),
            observer_sources: ObserverSources::default(),
        })
    }

    fn with_base(base_root: PathBuf) -> Result<Self, String> {
        fs::create_dir_all(&base_root).map_err(|_| "SCREEN_RESOURCE_ROOT_UNAVAILABLE")?;
        restrict_directory(&base_root)?;
        let base_root = base_root
            .canonicalize()
            .map_err(|_| "SCREEN_RESOURCE_ROOT_UNAVAILABLE")?;
        Ok(Self {
            base_root,
            available: true,
            state: Mutex::new(CaptureState::default()),
            focus_revision: AtomicU64::new(0),
            observer_sources: ObserverSources::default(),
        })
    }

    pub fn focus_revision(&self) -> u64 {
        self.focus_revision.load(Ordering::SeqCst)
    }

    pub fn set_awareness_mode(
        &self,
        generation_id: &str,
        personal: bool,
        revision: u64,
    ) -> Result<(), String> {
        validate_generation(generation_id)?;
        let mut state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE")?;
        if revision != self.focus_revision() {
            return Err("SCREEN_OBSERVATION_TARGET_STALE".into());
        }
        if state.active_generation.as_deref() != Some(generation_id) {
            cleanup_resources(&mut state.resources);
            state.awareness_frames.clear();
            state.foreground_offer = None;
            state.active_generation = Some(generation_id.into());
        }
        state.foreground_only = personal;
        Ok(())
    }

    pub fn offer_foreground(
        &self,
        generation_id: &str,
        target: ForegroundTarget,
        visible_text: String,
        revision: u64,
    ) -> Result<String, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE")?;
        if revision != self.focus_revision()
            || state.active_generation.as_deref() != Some(generation_id)
            || !state.foreground_only
        {
            return Err("SCREEN_OBSERVATION_TARGET_STALE".into());
        }
        let ticket = Uuid::new_v4().simple().to_string();
        state.awareness_frames.clear();
        state.foreground_offer = Some(ForegroundOffer {
            generation_id: generation_id.into(),
            ticket: ticket.clone(),
            target,
            visible_text: visible_text.chars().take(2000).collect(),
            created_at: Instant::now(),
        });
        Ok(ticket)
    }

    pub fn check_foreground_offer(
        &self,
        generation_id: &str,
        request: &ScreenAwarenessOfferRequest,
        current: Option<&ForegroundTarget>,
        privacy: &ScreenPrivacy,
    ) -> Result<ForegroundOffer, String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE")?;
        let offer = state
            .foreground_offer
            .as_ref()
            .filter(|offer| {
                state.active_generation.as_deref() == Some(generation_id)
                    && request.scope == generation_id
                    && offer.generation_id == generation_id
                    && request.capture_ticket == offer.ticket
                    && offer.created_at.elapsed() < RESOURCE_TTL
                    && current == Some(&offer.target)
            })
            .ok_or("SCREEN_OBSERVATION_TARGET_STALE")?;
        if let Some(code) =
            screen_observation_block(privacy, Some(&offer.target.window), std::process::id())
        {
            return Err(code.into());
        }
        Ok(offer.clone())
    }

    pub fn require_foreground_offer(
        &self,
        request: Option<&ScreenAwarenessOfferRequest>,
    ) -> Result<(), String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE")?;
        if state.foreground_only && request.is_none() {
            return Err("SCREEN_OBSERVATION_TARGET_STALE".into());
        }
        Ok(())
    }

    pub fn begin_session(
        &self,
        generation_id: &str,
        monitors: &[CaptureMonitor],
    ) -> Result<(String, Vec<String>, Vec<String>), String> {
        if !self.available {
            return Err("SCREEN_RESOURCE_ROOT_UNAVAILABLE".to_string());
        }
        validate_generation(generation_id)?;
        if monitors.is_empty() {
            return Err("SCREEN_CAPTURE_NO_MONITORS".to_string());
        }
        self.cleanup_expired();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string())?;
        let previous = state
            .active
            .take()
            .map(|session| session.windows.into_keys().collect())
            .unwrap_or_default();
        if state.active_generation.as_deref() != Some(generation_id) {
            cleanup_resources(&mut state.resources);
            state.awareness_frames.clear();
            state.foreground_offer = None;
            state.active_generation = Some(generation_id.to_string());
        }
        let session_id = Uuid::new_v4().simple().to_string();
        let mut windows = HashMap::new();
        let mut labels = Vec::with_capacity(monitors.len());
        for (index, monitor) in monitors.iter().enumerate() {
            let label = format!("capture-{}-{index}", &session_id[..12]);
            windows.insert(label.clone(), monitor.id);
            labels.push(label);
        }
        state.active = Some(CaptureSession {
            id: session_id.clone(),
            generation_id: generation_id.to_string(),
            windows,
        });
        Ok((session_id, labels, previous))
    }

    pub fn claim_selection(
        &self,
        session_id: &str,
        window_label: &str,
        monitor_id: u32,
    ) -> Result<CaptureClaim, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string())?;
        let session = state
            .active
            .take()
            .ok_or_else(|| "SCREEN_CAPTURE_SESSION_STALE".to_string())?;
        if session.id != session_id
            || session.windows.get(window_label).copied() != Some(monitor_id)
        {
            state.active = Some(session);
            return Err("SCREEN_CAPTURE_SESSION_STALE".to_string());
        }
        Ok(CaptureClaim {
            generation_id: session.generation_id,
            monitor_id,
            window_labels: session.windows.into_keys().collect(),
        })
    }

    pub fn cancel_session(&self, session_id: &str, window_label: &str) -> Option<Vec<String>> {
        let mut state = self.state.lock().ok()?;
        let matches = state.active.as_ref().is_some_and(|session| {
            session.id == session_id && session.windows.contains_key(window_label)
        });
        matches.then(|| {
            state
                .active
                .take()
                .expect("matched session exists")
                .windows
                .into_keys()
                .collect()
        })
    }

    pub fn capture(
        &self,
        claim: &CaptureClaim,
        local_rect: PhysicalRect,
    ) -> Result<ScreenResourceDescriptor, String> {
        self.cleanup_expired();
        let monitor = Monitor::all()
            .map_err(|_| "SCREEN_CAPTURE_PLATFORM_UNAVAILABLE".to_string())?
            .into_iter()
            .find(|monitor| monitor.id().ok() == Some(claim.monitor_id))
            .ok_or_else(|| "SCREEN_CAPTURE_MONITOR_GONE".to_string())?;
        let monitor_width = monitor
            .width()
            .map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE".to_string())?;
        let monitor_height = monitor
            .height()
            .map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE".to_string())?;
        let rect = intersect_local(local_rect, monitor_width, monitor_height)
            .ok_or_else(|| "SCREEN_CAPTURE_SELECTION_INVALID".to_string())?;
        let image = monitor
            .capture_region(rect.x as u32, rect.y as u32, rect.width, rect.height)
            .map_err(|_| "SCREEN_CAPTURE_PLATFORM_DENIED".to_string())?;
        let image = resize_capture(image);
        let rgb = image::DynamicImage::ImageRgba8(image).to_rgb8();
        let mut bytes = Vec::new();
        JpegEncoder::new_with_quality(&mut bytes, 70)
            .write_image(
                rgb.as_raw(),
                rgb.width(),
                rgb.height(),
                ExtendedColorType::Rgb8,
            )
            .map_err(|_| "SCREEN_CAPTURE_ENCODE_FAILED".to_string())?;
        if bytes.is_empty() || bytes.len() > MAX_CAPTURE_BYTES {
            return Err("SCREEN_CAPTURE_RESOURCE_LIMIT".to_string());
        }
        let root = self.generation_root(&claim.generation_id)?;
        let token = Uuid::new_v4().simple().to_string();
        let path = root.join(format!("{token}.jpg"));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|_| "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string())?;
        restrict_file(&path)?;
        if file.write_all(&bytes).and_then(|_| file.flush()).is_err() {
            let _ = fs::remove_file(&path);
            return Err("SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string());
        }
        let canonical = path
            .canonicalize()
            .map_err(|_| "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string())?;
        if canonical.parent() != Some(root.as_path()) {
            let _ = fs::remove_file(&canonical);
            return Err("SCREEN_CAPTURE_RESOURCE_ESCAPE".to_string());
        }
        let mut state = match self.state.lock() {
            Ok(state) => state,
            Err(_) => {
                let _ = fs::remove_file(&canonical);
                return Err("SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string());
            }
        };
        state.resources.insert(
            token.clone(),
            CaptureResource {
                path: canonical,
                generation_id: claim.generation_id.clone(),
                created_at: Instant::now(),
            },
        );
        Ok(ScreenResourceDescriptor {
            generation_id: claim.generation_id.clone(),
            resource_token: token,
            mime_type: "image/jpeg",
            width: rgb.width(),
            height: rgb.height(),
            byte_length: bytes.len(),
            captured_at: OffsetDateTime::now_utc()
                .format(&Rfc3339)
                .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string()),
            screen_name: monitor
                .name()
                .ok()
                .filter(|name| !name.trim().is_empty())
                .unwrap_or_else(|| format!("monitor-{}", claim.monitor_id))
                .chars()
                .take(128)
                .collect(),
        })
    }

    pub fn release(&self, token: &str, generation_id: &str) {
        let resource = self.state.lock().ok().and_then(|mut state| {
            let matches = state
                .resources
                .get(token)
                .is_some_and(|resource| resource.generation_id == generation_id);
            matches.then(|| state.resources.remove(token)).flatten()
        });
        if let Some(resource) = resource {
            let _ = fs::remove_file(resource.path);
        }
    }

    pub fn capture_screen_awareness_frame(
        &self,
        generation_id: &str,
        cursor_x: i32,
        cursor_y: i32,
        resolution: &str,
        batch_limit: usize,
        privacy: &ScreenPrivacy,
        offer_request: Option<&ScreenAwarenessOfferRequest>,
    ) -> Result<ScreenAwarenessCapturePublication, String> {
        if !self.available {
            return Err("SCREEN_RESOURCE_ROOT_UNAVAILABLE".to_string());
        }
        validate_generation(generation_id)?;
        if !(1..=20).contains(&batch_limit) || !valid_screen_awareness_resolution(resolution) {
            return Err("SCREEN_AWARENESS_SETTINGS_INVALID".to_string());
        }
        self.require_foreground_offer(offer_request)?;
        if let Some(code) =
            screen_observation_block(privacy, foreground_window().as_ref(), std::process::id())
        {
            return Err(code.to_string());
        }
        let (image, screen_name) = if let Some(request) = offer_request {
            let offer = self.check_foreground_offer(
                generation_id,
                request,
                observer_sources::foreground_target().as_ref(),
                privacy,
            )?;
            let monitors = Monitor::all().map_err(|_| "SCREEN_CAPTURE_NO_MONITORS")?;
            let bounds: Vec<_> = monitors
                .iter()
                .map(|monitor| {
                    Ok(PhysicalRect {
                        x: monitor.x().map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE")?,
                        y: monitor.y().map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE")?,
                        width: monitor.width().map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE")?,
                        height: monitor
                            .height()
                            .map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE")?,
                    })
                })
                .collect::<Result<_, String>>()?;
            let image = observer_sources::compose_foreground(
                offer.target.bounds,
                &bounds,
                |index, rect| {
                    monitors[index]
                        .capture_region(rect.x as u32, rect.y as u32, rect.width, rect.height)
                        .map_err(|_| "SCREEN_CAPTURE_PLATFORM_DENIED".into())
                },
            )?;
            self.check_foreground_offer(
                generation_id,
                request,
                observer_sources::foreground_target().as_ref(),
                privacy,
            )?;
            (image, "foreground".into())
        } else {
            let monitor = Monitor::from_point(cursor_x, cursor_y)
                .map_err(|_| "SCREEN_CAPTURE_MONITOR_GONE")?;
            let image = monitor
                .capture_image()
                .map_err(|_| "SCREEN_CAPTURE_PLATFORM_DENIED")?;
            (
                image,
                monitor
                    .name()
                    .ok()
                    .filter(|name| !name.trim().is_empty())
                    .unwrap_or_else(|| "monitor".into())
                    .chars()
                    .take(128)
                    .collect(),
            )
        };
        let image = resize_screen_awareness_capture(image, resolution);
        if u64::from(image.width()) * u64::from(image.height()) > MAX_CAPTURE_PIXELS {
            return Err("SCREEN_CAPTURE_RESOURCE_LIMIT".to_string());
        }
        let rgb = image::DynamicImage::ImageRgba8(image).to_rgb8();
        let mut bytes = Vec::new();
        JpegEncoder::new_with_quality(&mut bytes, 70)
            .write_image(
                rgb.as_raw(),
                rgb.width(),
                rgb.height(),
                ExtendedColorType::Rgb8,
            )
            .map_err(|_| "SCREEN_CAPTURE_ENCODE_FAILED".to_string())?;
        if bytes.is_empty() || bytes.len() > MAX_CAPTURE_BYTES {
            return Err("SCREEN_CAPTURE_RESOURCE_LIMIT".to_string());
        }
        let frame = ScreenAwarenessFrame {
            bytes,
            width: rgb.width(),
            height: rgb.height(),
            captured_at: OffsetDateTime::now_utc()
                .format(&Rfc3339)
                .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string()),
            screen_name,
        };
        if offer_request.is_some() {
            self.push_screen_awareness_frame_checked(
                generation_id,
                frame,
                batch_limit,
                offer_request,
            )
        } else {
            self.push_screen_awareness_frame(generation_id, frame, batch_limit)
        }
    }

    fn push_screen_awareness_frame(
        &self,
        generation_id: &str,
        frame: ScreenAwarenessFrame,
        batch_limit: usize,
    ) -> Result<ScreenAwarenessCapturePublication, String> {
        self.push_screen_awareness_frame_checked(generation_id, frame, batch_limit, None)
    }

    fn push_screen_awareness_frame_checked(
        &self,
        generation_id: &str,
        frame: ScreenAwarenessFrame,
        batch_limit: usize,
        offer: Option<&ScreenAwarenessOfferRequest>,
    ) -> Result<ScreenAwarenessCapturePublication, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string())?;
        if let Some(request) = offer {
            if !state.foreground_offer.as_ref().is_some_and(|offer| {
                offer.generation_id == generation_id
                    && request.scope == generation_id
                    && request.capture_ticket == offer.ticket
                    && offer.created_at.elapsed() < RESOURCE_TTL
            }) {
                return Err("SCREEN_OBSERVATION_TARGET_STALE".into());
            }
        }
        if state.active_generation.as_deref() != Some(generation_id) {
            cleanup_resources(&mut state.resources);
            state.awareness_frames.clear();
            state.active_generation = Some(generation_id.to_string());
        }
        state.awareness_frames.push_back(frame);
        let mut dropped_count = 0;
        while state.awareness_frames.len() > batch_limit
            || awareness_batch_bytes(&state.awareness_frames) > MAX_SCREEN_AWARENESS_BATCH_BYTES
        {
            state.awareness_frames.pop_front();
            dropped_count += 1;
        }
        Ok(ScreenAwarenessCapturePublication {
            count: state.awareness_frames.len(),
            dropped_count,
        })
    }

    pub fn materialize_screen_awareness_batch(
        &self,
        generation_id: &str,
    ) -> Result<Vec<ScreenResourceDescriptor>, String> {
        validate_generation(generation_id)?;
        self.cleanup_expired();
        let frames = {
            let mut state = self
                .state
                .lock()
                .map_err(|_| "SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string())?;
            if state.active_generation.as_deref() != Some(generation_id) {
                state.awareness_frames.clear();
                return Err("SCREEN_CAPTURE_GENERATION_STALE".to_string());
            }
            state.awareness_frames.drain(..).collect::<Vec<_>>()
        };
        if frames.is_empty() {
            return Err("SCREEN_AWARENESS_BATCH_EMPTY".to_string());
        }
        let root = self.generation_root(generation_id)?;
        let mut descriptors = Vec::with_capacity(frames.len());
        for frame in frames {
            let token = Uuid::new_v4().simple().to_string();
            let path = root.join(format!("{token}.jpg"));
            let write_result = (|| {
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&path)
                    .map_err(|_| "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string())?;
                restrict_file(&path)?;
                file.write_all(&frame.bytes)
                    .and_then(|_| file.flush())
                    .map_err(|_| "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string())?;
                path.canonicalize()
                    .map_err(|_| "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string())
            })();
            let canonical = match write_result {
                Ok(path) if path.parent() == Some(root.as_path()) => path,
                Ok(path) => {
                    let _ = fs::remove_file(path);
                    self.release_descriptors(&descriptors, generation_id);
                    return Err("SCREEN_CAPTURE_RESOURCE_ESCAPE".to_string());
                }
                Err(error) => {
                    let _ = fs::remove_file(&path);
                    self.release_descriptors(&descriptors, generation_id);
                    return Err(error);
                }
            };
            let descriptor = ScreenResourceDescriptor {
                generation_id: generation_id.to_string(),
                resource_token: token.clone(),
                mime_type: "image/jpeg",
                width: frame.width,
                height: frame.height,
                byte_length: frame.bytes.len(),
                captured_at: frame.captured_at,
                screen_name: frame.screen_name,
            };
            let mut state = match self.state.lock() {
                Ok(state) => state,
                Err(_) => {
                    let _ = fs::remove_file(&canonical);
                    self.release_descriptors(&descriptors, generation_id);
                    return Err("SCREEN_CAPTURE_STATE_UNAVAILABLE".to_string());
                }
            };
            state.resources.insert(
                token,
                CaptureResource {
                    path: canonical,
                    generation_id: generation_id.to_string(),
                    created_at: Instant::now(),
                },
            );
            descriptors.push(descriptor);
        }
        Ok(descriptors)
    }

    pub fn clear_screen_awareness_batch(&self) -> usize {
        self.state
            .lock()
            .map(|mut state| {
                let count = state.awareness_frames.len();
                state.awareness_frames.clear();
                state.foreground_offer = None;
                self.focus_revision.fetch_add(1, Ordering::SeqCst);
                self.observer_sources.invalidate();
                count
            })
            .unwrap_or(0)
    }

    pub fn release_descriptors(
        &self,
        descriptors: &[ScreenResourceDescriptor],
        generation_id: &str,
    ) {
        for descriptor in descriptors {
            self.release(&descriptor.resource_token, generation_id);
        }
    }

    fn generation_root(&self, generation_id: &str) -> Result<PathBuf, String> {
        validate_generation(generation_id)?;
        let root = self.base_root.join(generation_id);
        fs::create_dir_all(&root).map_err(|_| "SCREEN_RESOURCE_ROOT_UNAVAILABLE".to_string())?;
        restrict_directory(&root)?;
        let root = root
            .canonicalize()
            .map_err(|_| "SCREEN_RESOURCE_ROOT_UNAVAILABLE".to_string())?;
        if root.parent() != Some(self.base_root.as_path()) {
            return Err("SCREEN_CAPTURE_RESOURCE_ESCAPE".to_string());
        }
        Ok(root)
    }

    fn cleanup_expired(&self) {
        let expired = self.state.lock().ok().map(|mut state| {
            let now = Instant::now();
            let tokens = state
                .resources
                .iter()
                .filter(|(_, resource)| {
                    now.checked_duration_since(resource.created_at)
                        .is_some_and(|age| age >= RESOURCE_TTL)
                })
                .map(|(token, _)| token.clone())
                .collect::<Vec<_>>();
            tokens
                .into_iter()
                .filter_map(|token| state.resources.remove(&token))
                .collect::<Vec<_>>()
        });
        for resource in expired.unwrap_or_default() {
            let _ = fs::remove_file(resource.path);
        }
    }
}

impl Drop for CaptureManager {
    fn drop(&mut self) {
        self.observer_sources.shutdown();
        if let Ok(state) = self.state.get_mut() {
            cleanup_resources(&mut state.resources);
        }
    }
}

pub fn monitor_descriptors() -> Result<Vec<CaptureMonitor>, String> {
    Monitor::all()
        .map_err(|_| "SCREEN_CAPTURE_PLATFORM_UNAVAILABLE".to_string())?
        .into_iter()
        .map(|monitor| {
            let id = monitor
                .id()
                .map_err(|_| "SCREEN_CAPTURE_MONITOR_INVALID".to_string())?;
            Ok(CaptureMonitor {
                id,
                name: monitor.name().unwrap_or_else(|_| format!("monitor-{id}")),
                bounds: PhysicalRect {
                    x: monitor
                        .x()
                        .map_err(|_| "SCREEN_CAPTURE_MONITOR_INVALID".to_string())?,
                    y: monitor
                        .y()
                        .map_err(|_| "SCREEN_CAPTURE_MONITOR_INVALID".to_string())?,
                    width: monitor
                        .width()
                        .map_err(|_| "SCREEN_CAPTURE_MONITOR_INVALID".to_string())?,
                    height: monitor
                        .height()
                        .map_err(|_| "SCREEN_CAPTURE_MONITOR_INVALID".to_string())?,
                },
                primary: monitor.is_primary().unwrap_or(false),
            })
        })
        .collect()
}

pub fn show_overlays(
    app: &AppHandle,
    session_id: &str,
    labels: &[String],
    monitors: &[CaptureMonitor],
    theme_primary: &str,
) -> Result<(), String> {
    if labels.len() != monitors.len() {
        return Err("SCREEN_CAPTURE_OVERLAY_INVALID".to_string());
    }
    let creation_scale = app
        .primary_monitor()
        .ok()
        .flatten()
        .map(|monitor| monitor.scale_factor())
        .filter(|scale| scale.is_finite() && *scale > 0.0)
        .unwrap_or(1.0);
    let mut created = Vec::new();
    for (label, monitor) in labels.iter().zip(monitors) {
        let query = capture_overlay_url(session_id, monitor.id, theme_primary);
        let initial_size = initial_overlay_size(monitor, creation_scale);
        // WebView2 binds its composition controller to the monitor that owns the
        // parent HWND when the controller is created. Building a WebviewWindow at
        // its default position and moving it afterwards can therefore leave a
        // secondary-monitor controller with an opaque white backing surface.
        // Create the hidden native window at its final physical size, place it,
        // then attach the transparent webview after the HWND reaches its monitor.
        // This also avoids resizing Tauri's default 800x600 transparent surface,
        // which can leave the newly exposed area opaque on some Windows displays.
        // Tao also enables shadows for undecorated windows by default; that
        // non-client surface can remain opaque white on some Windows displays.
        let window = match WindowBuilder::new(app, label)
            .title(format!("Sakura 截图 · {}", monitor.name))
            .inner_size(initial_size.width, initial_size.height)
            .decorations(false)
            .shadow(false)
            .transparent(true)
            .always_on_top(true)
            .skip_taskbar(true)
            .resizable(false)
            .maximizable(false)
            .minimizable(false)
            .closable(false)
            .visible(false)
            .build()
        {
            Ok(window) => window,
            Err(_) => {
                close_windows(app, &created);
                return Err("SCREEN_CAPTURE_OVERLAY_UNAVAILABLE".to_string());
            }
        };
        if window
            .set_position(PhysicalPosition::new(monitor.bounds.x, monitor.bounds.y))
            .and_then(|_| window.set_size(overlay_size(monitor)))
            .is_err()
        {
            close_windows(app, &created);
            let _ = window.close();
            return Err("SCREEN_CAPTURE_OVERLAY_UNAVAILABLE".to_string());
        }
        let webview = WebviewBuilder::new(label, WebviewUrl::App(query.into()))
            .devtools(false)
            .transparent(true)
            .background_color(Color(0, 0, 0, 0))
            .focused(false)
            .auto_resize();
        if window
            .add_child(webview, PhysicalPosition::new(0, 0), overlay_size(monitor))
            .and_then(|_| window.show())
            .is_err()
        {
            close_windows(app, &created);
            let _ = window.close();
            return Err("SCREEN_CAPTURE_OVERLAY_UNAVAILABLE".to_string());
        }
        created.push(label.clone());
    }
    if let Some(primary_index) = monitors.iter().position(|monitor| monitor.primary) {
        if let Some(window) = app.get_webview_window(&labels[primary_index]) {
            let _ = window.set_focus();
        }
    }
    Ok(())
}

fn overlay_size(monitor: &CaptureMonitor) -> PhysicalSize<u32> {
    PhysicalSize::new(monitor.bounds.width, monitor.bounds.height)
}

fn initial_overlay_size(monitor: &CaptureMonitor, creation_scale: f64) -> LogicalSize<f64> {
    debug_assert!(creation_scale.is_finite() && creation_scale > 0.0);
    LogicalSize::new(
        f64::from(monitor.bounds.width) / creation_scale,
        f64::from(monitor.bounds.height) / creation_scale,
    )
}

fn capture_overlay_url(session_id: &str, monitor_id: u32, theme_primary: &str) -> String {
    let theme_primary = theme_primary
        .strip_prefix('#')
        .filter(|value| value.len() == 6 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .unwrap_or("4b9ac4");
    format!(
        "capture.html?sessionId={session_id}&monitorId={monitor_id}&themePrimary={theme_primary}"
    )
}

pub fn close_windows(app: &AppHandle, labels: &[String]) {
    for label in labels {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.close();
        }
    }
}

pub fn hide_windows(app: &AppHandle, labels: &[String]) {
    for label in labels {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.hide();
        }
    }
}

pub fn logical_selection_to_physical(
    window: &WebviewWindow,
    request: &CaptureSelectionRequest,
) -> Result<PhysicalRect, String> {
    if !request.x.is_finite()
        || !request.y.is_finite()
        || !request.width.is_finite()
        || !request.height.is_finite()
        || request.x < 0.0
        || request.y < 0.0
        || request.width < MIN_SELECTION_LOGICAL_PX
        || request.height < MIN_SELECTION_LOGICAL_PX
    {
        return Err("SCREEN_CAPTURE_SELECTION_INVALID".to_string());
    }
    let scale = window
        .scale_factor()
        .map_err(|_| "SCREEN_CAPTURE_SCALE_UNAVAILABLE".to_string())?;
    selection_at_scale(request, scale)
}

fn selection_at_scale(
    request: &CaptureSelectionRequest,
    scale: f64,
) -> Result<PhysicalRect, String> {
    if !scale.is_finite() || scale <= 0.0 {
        return Err("SCREEN_CAPTURE_SCALE_UNAVAILABLE".to_string());
    }
    Ok(PhysicalRect {
        x: (request.x * scale).round().max(0.0) as i32,
        y: (request.y * scale).round().max(0.0) as i32,
        width: (request.width * scale).round().max(1.0) as u32,
        height: (request.height * scale).round().max(1.0) as u32,
    })
}

fn intersect_local(rect: PhysicalRect, width: u32, height: u32) -> Option<PhysicalRect> {
    if rect.x < 0 || rect.y < 0 {
        return None;
    }
    let x = rect.x as u32;
    let y = rect.y as u32;
    let right = x.saturating_add(rect.width).min(width);
    let bottom = y.saturating_add(rect.height).min(height);
    (right > x && bottom > y).then_some(PhysicalRect {
        x: rect.x,
        y: rect.y,
        width: right - x,
        height: bottom - y,
    })
}

fn resize_capture(image: image::RgbaImage) -> image::RgbaImage {
    let longest = image.width().max(image.height());
    if longest <= MAX_CAPTURE_EDGE {
        return image;
    }
    let scale = MAX_CAPTURE_EDGE as f64 / longest as f64;
    image::imageops::resize(
        &image,
        (image.width() as f64 * scale).round().max(1.0) as u32,
        (image.height() as f64 * scale).round().max(1.0) as u32,
        FilterType::Lanczos3,
    )
}

fn valid_screen_awareness_resolution(value: &str) -> bool {
    matches!(value, "fullscreen" | "720p" | "1080p" | "2160p")
}

fn screen_awareness_target_size(width: u32, height: u32, resolution: &str) -> (u32, u32) {
    let bounds = match resolution {
        "720p" => Some((1280_u32, 720_u32)),
        "1080p" => Some((1920_u32, 1080_u32)),
        "2160p" => Some((3840_u32, 2160_u32)),
        _ => None,
    };
    let Some((mut max_width, mut max_height)) = bounds else {
        return (width, height);
    };
    if height > width {
        std::mem::swap(&mut max_width, &mut max_height);
    }
    let scale = (max_width as f64 / width as f64)
        .min(max_height as f64 / height as f64)
        .min(1.0);
    (
        (width as f64 * scale).round().max(1.0) as u32,
        (height as f64 * scale).round().max(1.0) as u32,
    )
}

fn resize_screen_awareness_capture(image: image::RgbaImage, resolution: &str) -> image::RgbaImage {
    let target = screen_awareness_target_size(image.width(), image.height(), resolution);
    if target == (image.width(), image.height()) {
        image
    } else {
        image::imageops::resize(&image, target.0, target.1, FilterType::Lanczos3)
    }
}

fn awareness_batch_bytes(frames: &VecDeque<ScreenAwarenessFrame>) -> usize {
    frames.iter().map(|frame| frame.bytes.len()).sum()
}

fn validate_generation(value: &str) -> Result<(), String> {
    let valid = (8..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() || byte == b'-');
    valid
        .then_some(())
        .ok_or_else(|| "SCREEN_RESOURCE_GENERATION_INVALID".to_string())
}

fn cleanup_resources(resources: &mut HashMap<String, CaptureResource>) {
    for (_, resource) in resources.drain() {
        let _ = fs::remove_file(resource.path);
    }
}

#[cfg(unix)]
fn restrict_directory(path: &std::path::Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|_| "SCREEN_RESOURCE_ROOT_UNAVAILABLE".to_string())
}

#[cfg(not(unix))]
fn restrict_directory(_path: &std::path::Path) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
fn restrict_file(path: &std::path::Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).map_err(|_| {
        let _ = fs::remove_file(path);
        "SCREEN_CAPTURE_RESOURCE_WRITE_FAILED".to_string()
    })
}

#[cfg(not(unix))]
fn restrict_file(_path: &std::path::Path) -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn awareness_frame(label: &str, byte_length: usize) -> ScreenAwarenessFrame {
        ScreenAwarenessFrame {
            bytes: vec![7; byte_length],
            width: 100,
            height: 50,
            captured_at: label.to_string(),
            screen_name: "fixture".to_string(),
        }
    }

    fn request() -> CaptureSelectionRequest {
        CaptureSelectionRequest {
            session_id: "session".to_string(),
            monitor_id: 7,
            x: 10.0,
            y: 12.0,
            width: 80.0,
            height: 40.0,
        }
    }

    #[test]
    fn attachment_and_item_identifiers_use_distinct_opaque_prefixes() {
        assert!(valid_attachment_id(&format!("screen-{}", "a".repeat(32))));
        assert!(valid_attachment_item_id(&format!(
            "shot-{}",
            "b".repeat(32)
        )));
        assert!(!valid_attachment_id(&format!("shot-{}", "b".repeat(32))));
        assert!(!valid_attachment_item_id(&format!(
            "screen-{}",
            "a".repeat(32)
        )));
        assert!(!valid_attachment_item_id(&format!(
            "shot-{}",
            "g".repeat(32)
        )));
    }

    #[test]
    fn each_overlay_uses_its_own_scale_factor() {
        assert_eq!(
            selection_at_scale(&request(), 1.0).unwrap(),
            PhysicalRect {
                x: 10,
                y: 12,
                width: 80,
                height: 40
            }
        );
        assert_eq!(
            selection_at_scale(&request(), 1.5).unwrap(),
            PhysicalRect {
                x: 15,
                y: 18,
                width: 120,
                height: 60
            }
        );
    }

    #[test]
    fn overlay_native_surface_starts_at_the_target_monitor_size() {
        let monitor = CaptureMonitor {
            id: 7,
            name: "fixture".to_string(),
            bounds: PhysicalRect {
                x: 1920,
                y: 0,
                width: 2560,
                height: 1440,
            },
            primary: false,
        };
        let creation_scale = 1.25;
        let logical = initial_overlay_size(&monitor, creation_scale);

        assert_eq!(
            logical.to_physical::<u32>(creation_scale),
            overlay_size(&monitor)
        );
    }

    #[test]
    fn capture_overlay_uses_validated_theme_primary() {
        assert_eq!(
            capture_overlay_url("session", 7, "#A1b2C3"),
            "capture.html?sessionId=session&monitorId=7&themePrimary=A1b2C3"
        );
        assert_eq!(
            capture_overlay_url("session", 7, "#bad&injected=true"),
            "capture.html?sessionId=session&monitorId=7&themePrimary=4b9ac4"
        );
    }

    #[test]
    fn monitor_local_selection_clamps_at_the_monitor_edge() {
        assert_eq!(
            intersect_local(
                PhysicalRect {
                    x: 90,
                    y: 70,
                    width: 30,
                    height: 40
                },
                100,
                80
            ),
            Some(PhysicalRect {
                x: 90,
                y: 70,
                width: 10,
                height: 10
            })
        );
        assert!(intersect_local(
            PhysicalRect {
                x: -1,
                y: 0,
                width: 10,
                height: 10
            },
            100,
            80
        )
        .is_none());
    }

    #[test]
    fn sessions_are_generation_scoped_and_single_claim() {
        let root =
            std::env::temp_dir().join(format!("sakura-capture-test-{}", Uuid::new_v4().simple()));
        let manager = CaptureManager::with_base(root.clone()).unwrap();
        let monitor = CaptureMonitor {
            id: 7,
            name: "fixture".to_string(),
            bounds: PhysicalRect {
                x: -1920,
                y: 0,
                width: 1920,
                height: 1080,
            },
            primary: false,
        };
        let (session, labels, _) = manager
            .begin_session("00000000-0000-4000-8000-000000004006", &[monitor])
            .unwrap();
        let claim = manager.claim_selection(&session, &labels[0], 7).unwrap();
        assert_eq!(claim.monitor_id, 7);
        assert!(manager.claim_selection(&session, &labels[0], 7).is_err());
        drop(manager);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn expired_resources_are_removed_from_registry_and_disk() {
        let root =
            std::env::temp_dir().join(format!("sakura-capture-test-{}", Uuid::new_v4().simple()));
        let manager = CaptureManager::with_base(root.clone()).unwrap();
        let generation_id = "00000000-0000-4000-8000-000000004006";
        let generation_root = manager.generation_root(generation_id).unwrap();
        let token = "a".repeat(32);
        let path = generation_root.join(format!("{token}.jpg"));
        fs::write(&path, b"expired").unwrap();
        manager.state.lock().unwrap().resources.insert(
            token.clone(),
            CaptureResource {
                path: path.clone(),
                generation_id: generation_id.to_string(),
                created_at: Instant::now()
                    .checked_sub(RESOURCE_TTL + Duration::from_secs(1))
                    .unwrap(),
            },
        );

        manager.cleanup_expired();

        assert!(!path.exists());
        assert!(!manager.state.lock().unwrap().resources.contains_key(&token));
        drop(manager);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn screen_awareness_resolution_preserves_aspect_ratio_and_never_upscales() {
        assert_eq!(
            screen_awareness_target_size(2560, 1440, "720p"),
            (1280, 720)
        );
        assert_eq!(
            screen_awareness_target_size(1000, 600, "2160p"),
            (1000, 600)
        );
        assert_eq!(
            screen_awareness_target_size(1440, 2560, "1080p"),
            (1080, 1920)
        );
        assert_eq!(
            screen_awareness_target_size(3840, 2160, "fullscreen"),
            (3840, 2160)
        );
    }

    #[test]
    fn privacy_payload_is_exact_and_casefolded() {
        let privacy = ScreenPrivacy::from_core_payload(&serde_json::json!({
            "schemaVersion": 1,
            "blockedProcesses": ["Vault.EXE", " "],
            "blockedTitleKeywords": ["Online Banking"],
        }))
        .unwrap();
        assert_eq!(privacy.blocked_processes, ["vault.exe"]);
        assert_eq!(privacy.blocked_title_keywords, ["online banking"]);
        for invalid in [
            serde_json::json!({"schemaVersion": 2, "blockedProcesses": [], "blockedTitleKeywords": []}),
            serde_json::json!({"schemaVersion": 1, "blockedProcesses": [7], "blockedTitleKeywords": []}),
            serde_json::json!({"schemaVersion": 1, "blockedProcesses": []}),
            serde_json::json!({
                "schemaVersion": 1, "blockedProcesses": [], "blockedTitleKeywords": [], "extra": true
            }),
        ] {
            assert!(
                ScreenPrivacy::from_core_payload(&invalid).is_err(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn scheduled_observation_skips_our_window_and_private_windows() {
        let privacy = ScreenPrivacy {
            blocked_processes: vec!["vault.exe".to_string()],
            blocked_title_keywords: vec!["online banking".to_string()],
        };
        let window = |pid: u32, process: &str, title: &str| ForegroundWindow {
            hwnd: u64::from(pid),
            process_id: pid,
            process_name: process.to_string(),
            title: title.to_string(),
        };
        assert_eq!(
            screen_observation_block(&privacy, Some(&window(7, "sakura.exe", "Sakura")), 7),
            Some(SCREEN_OBSERVATION_SELF)
        );
        assert_eq!(
            screen_observation_block(&privacy, Some(&window(8, "Vault.exe", "")), 7),
            Some(SCREEN_OBSERVATION_PRIVACY_BLOCKED)
        );
        assert_eq!(
            screen_observation_block(
                &privacy,
                Some(&window(8, "browser.exe", "My Online Banking - Browser")),
                7
            ),
            Some(SCREEN_OBSERVATION_PRIVACY_BLOCKED)
        );
        assert_eq!(
            screen_observation_block(&privacy, Some(&window(8, "editor.exe", "notes.txt")), 7),
            None
        );
        assert_eq!(screen_observation_block(&privacy, None, 7), None);
        let snapshot = focus_advance_snapshot(Some(&window(8, "editor.exe", "notes.txt")), 7)
            .expect("snapshot");
        assert_eq!(snapshot.get("title").and_then(|value| value.as_str()), Some("notes.txt"));
        assert_eq!(snapshot.get("ownProcess").and_then(|value| value.as_bool()), Some(false));
        assert!(focus_advance_snapshot(None, 7).is_none());
    }

    #[test]
    fn personal_capture_offer_cannot_be_reused_for_a_new_window_or_scope() {
        let root =
            std::env::temp_dir().join(format!("sakura-awareness-test-{}", Uuid::new_v4().simple()));
        let manager = CaptureManager::with_base(root.clone()).unwrap();
        let generation_id = "00000000-0000-4000-8000-000000004007";
        let target = crate::observer_sources::ForegroundTarget {
            window: ForegroundWindow {
                hwnd: 8,
                process_id: 8,
                process_name: "editor.exe".into(),
                title: "notes".into(),
            },
            bounds: PhysicalRect {
                x: -100,
                y: 0,
                width: 200,
                height: 100,
            },
        };
        let revision = manager.focus_revision();
        manager
            .set_awareness_mode(generation_id, true, revision)
            .unwrap();
        assert!(manager.require_foreground_offer(None).is_err());
        let first = manager
            .offer_foreground(generation_id, target.clone(), "body".into(), revision)
            .unwrap();
        let request = ScreenAwarenessOfferRequest {
            scope: generation_id.into(),
            capture_ticket: first,
        };
        let privacy = ScreenPrivacy::default();
        assert!(manager
            .check_foreground_offer(generation_id, &request, Some(&target), &privacy)
            .is_ok());
        let mut changed = target.clone();
        changed.window.hwnd = 9;
        assert!(manager
            .check_foreground_offer(generation_id, &request, Some(&changed), &privacy)
            .is_err());
        let mut changed = target.clone();
        changed.window.process_id = 9;
        assert!(manager
            .check_foreground_offer(generation_id, &request, Some(&changed), &privacy)
            .is_err());
        let mut changed = target.clone();
        changed.bounds.width += 1;
        assert!(manager
            .check_foreground_offer(generation_id, &request, Some(&changed), &privacy)
            .is_err());
        assert!(manager
            .check_foreground_offer(
                generation_id,
                &request,
                Some(&target),
                &ScreenPrivacy {
                    blocked_processes: vec!["editor.exe".into()],
                    blocked_title_keywords: vec![],
                }
            )
            .is_err());
        assert!(manager
            .check_foreground_offer(generation_id, &request, None, &privacy)
            .is_err());
        let mut wrong_scope = request.clone();
        wrong_scope.scope = "00000000-0000-4000-8000-000000004008".into();
        assert!(manager
            .check_foreground_offer(generation_id, &wrong_scope, Some(&target), &privacy)
            .is_err());
        manager
            .offer_foreground(generation_id, target.clone(), "new".into(), revision)
            .unwrap();
        assert!(manager
            .check_foreground_offer(generation_id, &request, Some(&target), &privacy)
            .is_err());
        manager.clear_screen_awareness_batch();
        assert!(manager
            .offer_foreground(generation_id, target, "late".into(), revision)
            .is_err());
        drop(manager);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn screen_awareness_batch_keeps_latest_frames_in_capture_order_and_cleans_files() {
        let root =
            std::env::temp_dir().join(format!("sakura-awareness-test-{}", Uuid::new_v4().simple()));
        let manager = CaptureManager::with_base(root.clone()).unwrap();
        let generation_id = "00000000-0000-4000-8000-000000004007";
        manager
            .push_screen_awareness_frame(generation_id, awareness_frame("first", 8), 2)
            .unwrap();
        manager
            .push_screen_awareness_frame(generation_id, awareness_frame("second", 8), 2)
            .unwrap();
        let publication = manager
            .push_screen_awareness_frame(generation_id, awareness_frame("third", 8), 2)
            .unwrap();
        assert_eq!(publication.count, 2);
        assert_eq!(publication.dropped_count, 1);

        let descriptors = manager
            .materialize_screen_awareness_batch(generation_id)
            .unwrap();
        assert_eq!(
            descriptors
                .iter()
                .map(|descriptor| descriptor.captured_at.as_str())
                .collect::<Vec<_>>(),
            ["second", "third"]
        );
        let paths = descriptors
            .iter()
            .map(|descriptor| {
                root.join(generation_id)
                    .join(format!("{}.jpg", descriptor.resource_token))
            })
            .collect::<Vec<_>>();
        assert!(paths.iter().all(|path| path.exists()));
        manager.release_descriptors(&descriptors, generation_id);
        assert!(paths.iter().all(|path| !path.exists()));
        drop(manager);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn screen_awareness_batch_enforces_memory_limit_and_generation_cleanup() {
        let root =
            std::env::temp_dir().join(format!("sakura-awareness-test-{}", Uuid::new_v4().simple()));
        let manager = CaptureManager::with_base(root.clone()).unwrap();
        let first_generation = "00000000-0000-4000-8000-000000004007";
        for label in ["one", "two", "three"] {
            manager
                .push_screen_awareness_frame(
                    first_generation,
                    awareness_frame(label, 23 * 1024 * 1024),
                    20,
                )
                .unwrap();
        }
        assert_eq!(manager.state.lock().unwrap().awareness_frames.len(), 2);
        let second_generation = "00000000-0000-4000-8000-000000004008";
        manager
            .push_screen_awareness_frame(second_generation, awareness_frame("new", 8), 20)
            .unwrap();
        let state = manager.state.lock().unwrap();
        assert_eq!(state.active_generation.as_deref(), Some(second_generation));
        assert_eq!(state.awareness_frames.len(), 1);
        drop(state);
        drop(manager);
        let _ = fs::remove_dir_all(root);
    }
}
