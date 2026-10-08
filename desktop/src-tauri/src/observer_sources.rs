//! Bounded foreground-only sources for the personal Observer.

use crate::capture::{ForegroundWindow, PhysicalRect};
#[cfg(any(windows, test))]
use std::collections::VecDeque;
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};

const SOURCE_BUDGET: Duration = Duration::from_millis(200);
const TEXT_LIMIT: usize = 2000;
#[cfg(any(windows, test))]
const CONTROL_LIMIT: usize = 500;
#[cfg(any(windows, test))]
const DEPTH_LIMIT: usize = 20;

#[derive(Clone, Debug, PartialEq)]
pub struct ForegroundTarget {
    pub window: ForegroundWindow,
    pub bounds: PhysicalRect,
}

pub fn intersection(a: PhysicalRect, b: PhysicalRect) -> Option<PhysicalRect> {
    let x = i64::from(a.x).max(i64::from(b.x));
    let y = i64::from(a.y).max(i64::from(b.y));
    let right = (i64::from(a.x) + i64::from(a.width)).min(i64::from(b.x) + i64::from(b.width));
    let bottom = (i64::from(a.y) + i64::from(a.height)).min(i64::from(b.y) + i64::from(b.height));
    if right <= x || bottom <= y {
        return None;
    }
    Some(PhysicalRect {
        x: i32::try_from(x).ok()?,
        y: i32::try_from(y).ok()?,
        width: u32::try_from(right - x).ok()?,
        height: u32::try_from(bottom - y).ok()?,
    })
}

pub fn compose_foreground(
    target: PhysicalRect,
    monitors: &[PhysicalRect],
    mut capture: impl FnMut(usize, PhysicalRect) -> Result<image::RgbaImage, String>,
) -> Result<image::RgbaImage, String> {
    if target.width == 0
        || target.height == 0
        || u64::from(target.width) * u64::from(target.height) > 32_000_000
    {
        return Err("SCREEN_CAPTURE_RESOURCE_LIMIT".into());
    }
    let pieces: Vec<_> = monitors
        .iter()
        .enumerate()
        .filter_map(|(index, monitor)| {
            intersection(target, *monitor).map(|rect| (index, monitor, rect))
        })
        .collect();
    if pieces.is_empty() {
        return Err("SCREEN_OBSERVATION_TARGET_UNAVAILABLE".into());
    }
    let mut output = image::RgbaImage::new(target.width, target.height);
    for (index, monitor, rect) in pieces {
        let local = PhysicalRect {
            x: (i64::from(rect.x) - i64::from(monitor.x)) as i32,
            y: (i64::from(rect.y) - i64::from(monitor.y)) as i32,
            width: rect.width,
            height: rect.height,
        };
        let image = capture(index, local)?;
        if image.dimensions() != (rect.width, rect.height) {
            return Err("SCREEN_CAPTURE_DIMENSIONS_INVALID".into());
        }
        image::imageops::replace(
            &mut output,
            &image,
            i64::from(rect.x) - i64::from(target.x),
            i64::from(rect.y) - i64::from(target.y),
        );
    }
    Ok(output)
}

#[cfg(windows)]
pub fn foreground_target() -> Option<ForegroundTarget> {
    use windows::Win32::{
        Foundation::{HWND, RECT},
        Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_EXTENDED_FRAME_BOUNDS},
        UI::WindowsAndMessaging::{IsIconic, IsWindow, IsWindowVisible},
    };
    let window = crate::capture::foreground_window()?;
    if window.process_id == 0 || window.process_name.is_empty() {
        return None;
    }
    let hwnd = HWND(window.hwnd as usize as *mut std::ffi::c_void);
    unsafe {
        if !IsWindow(Some(hwnd)).as_bool()
            || IsIconic(hwnd).as_bool()
            || !IsWindowVisible(hwnd).as_bool()
        {
            return None;
        }
        let mut rect = RECT::default();
        DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            &mut rect as *mut _ as *mut _,
            std::mem::size_of::<RECT>() as u32,
        )
        .ok()?;
        let bounds = rect_from_edges(rect.left, rect.top, rect.right, rect.bottom)?;
        // The HWND/PID must still denote the snapshot whose rectangle was read.
        if crate::capture::foreground_window().as_ref() != Some(&window) {
            return None;
        }
        Some(ForegroundTarget { window, bounds })
    }
}

#[cfg(not(windows))]
pub fn foreground_target() -> Option<ForegroundTarget> {
    None
}

#[cfg(windows)]
fn rect_from_edges(left: i32, top: i32, right: i32, bottom: i32) -> Option<PhysicalRect> {
    let width = u32::try_from(i64::from(right) - i64::from(left)).ok()?;
    let height = u32::try_from(i64::from(bottom) - i64::from(top)).ok()?;
    (width > 0 && height > 0).then_some(PhysicalRect {
        x: left,
        y: top,
        width,
        height,
    })
}

struct Budget {
    until: Instant,
    epoch: Arc<AtomicU64>,
    expected_epoch: u64,
    closed: Arc<AtomicBool>,
}
impl Budget {
    fn fresh(epoch: Arc<AtomicU64>, closed: Arc<AtomicBool>) -> Self {
        Self {
            until: Instant::now() + SOURCE_BUDGET,
            expected_epoch: epoch.load(Ordering::SeqCst),
            epoch,
            closed,
        }
    }
    fn current(&self) -> bool {
        !self.closed.load(Ordering::SeqCst)
            && self.epoch.load(Ordering::SeqCst) == self.expected_epoch
    }
    fn active(&self) -> bool {
        self.current() && Instant::now() < self.until
    }
}

#[cfg(any(windows, test))]
struct ControlMetadata {
    password: bool,
    offscreen: bool,
    bounds: PhysicalRect,
}
#[cfg(any(windows, test))]
trait VisibleTree {
    type Node;
    fn root(&mut self, target: &ForegroundTarget, budget: &Budget) -> Option<Self::Node>;
    fn metadata(&mut self, node: &Self::Node, budget: &Budget) -> Option<ControlMetadata>;
    fn text(&mut self, node: &Self::Node, remaining: usize, budget: &Budget) -> String;
    fn children(&mut self, node: &Self::Node, remaining: usize, budget: &Budget)
        -> Vec<Self::Node>;
}

#[cfg(any(windows, test))]
fn collect_visible_text(
    tree: &mut impl VisibleTree,
    target: &ForegroundTarget,
    budget: &Budget,
) -> String {
    if !budget.active() {
        return String::new();
    }
    let Some(root) = tree.root(target, budget) else {
        return String::new();
    };
    let mut queue = VecDeque::from([(root, 0)]);
    let mut visited = 0;
    let mut body = String::new();
    while let Some((node, depth)) = queue.pop_front() {
        if !budget.active() || visited >= CONTROL_LIMIT || body.chars().count() >= TEXT_LIMIT {
            break;
        }
        visited += 1;
        let Some(meta) = tree.metadata(&node, budget) else {
            continue;
        };
        if meta.password || meta.offscreen || intersection(meta.bounds, target.bounds).is_none() {
            continue;
        }
        // The top-level name is a window title; read only its visible child controls.
        if depth > 0 && budget.active() {
            let remaining =
                TEXT_LIMIT.saturating_sub(body.chars().count() + usize::from(!body.is_empty()));
            let text = tree.text(&node, remaining, budget);
            let text = text.trim();
            if !text.is_empty() && !body.lines().any(|line| line == text) {
                if !body.is_empty() {
                    body.push('\n');
                }
                body.extend(text.chars().take(remaining));
            }
        }
        if depth < DEPTH_LIMIT && budget.active() {
            let remaining = CONTROL_LIMIT.saturating_sub(visited + queue.len());
            queue.extend(
                tree.children(&node, remaining, budget)
                    .into_iter()
                    .take(remaining)
                    .map(|child| (child, depth + 1)),
            );
        }
    }
    if budget.current() {
        body
    } else {
        String::new()
    }
}

pub struct SourceResult {
    pub text: String,
    pub status: &'static str,
}
impl SourceResult {
    fn empty(status: &'static str) -> Self {
        Self {
            text: String::new(),
            status,
        }
    }
}
struct Job {
    target: ForegroundTarget,
    budget: Budget,
    result: mpsc::Sender<String>,
}
struct Collector {
    sender: mpsc::SyncSender<Job>,
    busy: Arc<AtomicBool>,
    epoch: Arc<AtomicU64>,
    closed: Arc<AtomicBool>,
}
impl Collector {
    fn spawn<F, P>(factory: F) -> std::io::Result<Self>
    where
        F: FnOnce() -> P + Send + 'static,
        P: FnMut(&ForegroundTarget, &Budget) -> String + 'static,
    {
        let (sender, receiver) = mpsc::sync_channel::<Job>(1);
        let busy = Arc::new(AtomicBool::new(false));
        let worker_busy = busy.clone();
        std::thread::Builder::new()
            .name("sakura-observer-uia".into())
            .spawn(move || {
                // COM and all provider interfaces are created and destroyed on this thread.
                let mut provider = factory();
                while let Ok(job) = receiver.recv() {
                    let text = if job.budget.active() {
                        provider(&job.target, &job.budget)
                    } else {
                        String::new()
                    };
                    let _ = job.result.send(if job.budget.current() {
                        text
                    } else {
                        String::new()
                    });
                    worker_busy.store(false, Ordering::SeqCst);
                }
            })?; // Dropping the JoinHandle deliberately avoids an unbounded provider join.
        Ok(Self {
            sender,
            busy,
            epoch: Arc::new(AtomicU64::new(0)),
            closed: Arc::new(AtomicBool::new(false)),
        })
    }
    fn collect(&self, target: ForegroundTarget) -> SourceResult {
        if self.closed.load(Ordering::SeqCst) {
            return SourceResult::empty("closed");
        }
        if self
            .busy
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return SourceResult::empty("busy");
        }
        let budget = Budget::fresh(self.epoch.clone(), self.closed.clone());
        let expected_epoch = budget.expected_epoch;
        let (result, receiver) = mpsc::channel();
        if self
            .sender
            .try_send(Job {
                target,
                budget,
                result,
            })
            .is_err()
        {
            self.busy.store(false, Ordering::SeqCst);
            return SourceResult::empty("unavailable");
        }
        match receiver.recv_timeout(SOURCE_BUDGET) {
            Ok(text)
                if !self.closed.load(Ordering::SeqCst)
                    && self.epoch.load(Ordering::SeqCst) == expected_epoch =>
            {
                SourceResult {
                    text: text.chars().take(TEXT_LIMIT).collect(),
                    status: "completed",
                }
            }
            Ok(_) => SourceResult::empty("stale"),
            Err(_) => SourceResult::empty("timeout"),
        }
    }
    fn invalidate(&self) {
        self.epoch.fetch_add(1, Ordering::SeqCst);
    }
    fn shutdown(&self) {
        self.closed.store(true, Ordering::SeqCst);
        self.invalidate();
    }
}

#[derive(Default)]
pub struct ObserverSources {
    collector: Mutex<Option<Arc<Collector>>>,
    closed: AtomicBool,
}
impl ObserverSources {
    pub fn collect(&self, target: ForegroundTarget) -> SourceResult {
        if self.closed.load(Ordering::SeqCst) {
            return SourceResult::empty("closed");
        }
        let collector = {
            let Ok(mut slot) = self.collector.lock() else {
                return SourceResult::empty("unavailable");
            };
            if self.closed.load(Ordering::SeqCst) {
                return SourceResult::empty("closed");
            }
            if slot.is_none() {
                match Collector::spawn(native_provider) {
                    Ok(worker) => *slot = Some(Arc::new(worker)),
                    Err(_) => {
                        self.closed.store(true, Ordering::SeqCst);
                        return SourceResult::empty("unavailable");
                    }
                }
            }
            slot.as_ref().unwrap().clone()
        };
        collector.collect(target)
    }
    pub fn invalidate(&self) {
        if let Ok(slot) = self.collector.lock() {
            if let Some(worker) = slot.as_ref() {
                worker.invalidate();
            }
        }
    }
    pub fn shutdown(&self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Ok(mut slot) = self.collector.lock() {
            if let Some(worker) = slot.take() {
                worker.shutdown();
            }
        }
    }
}
impl Drop for ObserverSources {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(windows)]
fn native_provider() -> impl FnMut(&ForegroundTarget, &Budget) -> String {
    let mut reader = windows_source::Reader::new();
    move |target, budget| {
        reader
            .as_mut()
            .map(|tree| collect_visible_text(tree, target, budget))
            .unwrap_or_default()
    }
}
#[cfg(not(windows))]
fn native_provider() -> impl FnMut(&ForegroundTarget, &Budget) -> String {
    |_, _| String::new()
}

#[cfg(windows)]
mod windows_source {
    use super::*;
    use windows::Win32::{
        Foundation::HWND,
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
            COINIT_MULTITHREADED,
        },
        UI::Accessibility::{
            CUIAutomation8, IUIAutomation2, IUIAutomationElement, IUIAutomationTextPattern,
            IUIAutomationTreeWalker, IUIAutomationValuePattern, UIA_TextPatternId,
            UIA_ValuePatternId,
        },
    };
    struct ComScope;
    impl Drop for ComScope {
        fn drop(&mut self) {
            unsafe {
                CoUninitialize();
            }
        }
    }
    pub(super) struct Reader {
        automation: IUIAutomation2,
        walker: IUIAutomationTreeWalker,
        _com: ComScope,
    }
    impl Reader {
        pub(super) fn new() -> Option<Self> {
            unsafe {
                CoInitializeEx(None, COINIT_MULTITHREADED).ok().ok()?;
                let com = ComScope;
                let automation: IUIAutomation2 =
                    CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER).ok()?;
                automation.SetConnectionTimeout(200).ok()?;
                automation.SetTransactionTimeout(200).ok()?;
                let walker = automation.ControlViewWalker().ok()?;
                Some(Self {
                    automation,
                    walker,
                    _com: com,
                })
            }
        }
    }
    impl VisibleTree for Reader {
        type Node = IUIAutomationElement;
        fn root(&mut self, target: &ForegroundTarget, budget: &Budget) -> Option<Self::Node> {
            if !budget.active() || foreground_target().as_ref() != Some(target) {
                return None;
            }
            unsafe {
                self.automation
                    .ElementFromHandle(HWND(target.window.hwnd as usize as *mut _))
                    .ok()
            }
        }
        fn metadata(&mut self, node: &Self::Node, budget: &Budget) -> Option<ControlMetadata> {
            unsafe {
                if !budget.active() {
                    return None;
                }
                let password = node.CurrentIsPassword().ok()?.as_bool();
                if password || !budget.active() {
                    return None;
                }
                let offscreen = node.CurrentIsOffscreen().ok()?.as_bool();
                if offscreen || !budget.active() {
                    return None;
                }
                let rect = node.CurrentBoundingRectangle().ok()?;
                Some(ControlMetadata {
                    password,
                    offscreen,
                    bounds: rect_from_edges(rect.left, rect.top, rect.right, rect.bottom)?,
                })
            }
        }
        fn text(&mut self, node: &Self::Node, remaining: usize, budget: &Budget) -> String {
            let mut text = String::new();
            unsafe {
                if !budget.active() || remaining == 0 {
                    return text;
                }
                // GetVisibleRanges avoids reading DocumentRange's hidden or scrolled body.
                if let Ok(pattern) =
                    node.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId)
                {
                    if budget.active() {
                        if let Ok(ranges) = pattern.GetVisibleRanges() {
                            if !budget.active() {
                                return text;
                            }
                            for index in 0..ranges.Length().unwrap_or(0).min(CONTROL_LIMIT as i32) {
                                if !budget.active() || text.chars().count() >= remaining {
                                    break;
                                }
                                let Ok(range) = ranges.GetElement(index) else {
                                    continue;
                                };
                                if !budget.active() {
                                    break;
                                }
                                if let Ok(value) =
                                    range.GetText((remaining - text.chars().count()) as i32)
                                {
                                    text.extend(
                                        value
                                            .to_string()
                                            .chars()
                                            .take(remaining - text.chars().count()),
                                    );
                                }
                            }
                        }
                    }
                }
                if text.is_empty() && budget.active() {
                    if let Ok(pattern) =
                        node.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
                    {
                        if budget.active() {
                            if let Ok(value) = pattern.CurrentValue() {
                                text.extend(value.to_string().chars().take(remaining));
                            }
                        }
                    }
                }
                if text.is_empty() && budget.active() {
                    if let Ok(name) = node.CurrentName() {
                        text.extend(name.to_string().chars().take(remaining));
                    }
                }
            }
            text
        }
        fn children(
            &mut self,
            node: &Self::Node,
            remaining: usize,
            budget: &Budget,
        ) -> Vec<Self::Node> {
            let mut children = Vec::new();
            if remaining == 0 || !budget.active() {
                return children;
            }
            unsafe {
                let mut child = self.walker.GetFirstChildElement(node).ok();
                while let Some(element) = child {
                    if children.len() >= remaining || !budget.active() {
                        break;
                    }
                    child = self.walker.GetNextSiblingElement(&element).ok();
                    children.push(element);
                }
            }
            children
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::{ForegroundWindow, PhysicalRect};
    use std::sync::{mpsc, Arc};

    fn target() -> ForegroundTarget {
        ForegroundTarget {
            window: ForegroundWindow {
                hwnd: 7,
                process_id: 8,
                process_name: "editor.exe".into(),
                title: "notes".into(),
            },
            bounds: PhysicalRect {
                x: -100,
                y: 20,
                width: 200,
                height: 50,
            },
        }
    }

    #[test]
    fn foreground_region_uses_signed_physical_monitor_coordinates() {
        let monitors = [
            PhysicalRect {
                x: -1920,
                y: 0,
                width: 1920,
                height: 1080,
            },
            PhysicalRect {
                x: 0,
                y: 0,
                width: 1920,
                height: 1080,
            },
        ];
        let mut regions = Vec::new();
        let image = compose_foreground(target().bounds, &monitors, |index, rect| {
            regions.push((index, rect));
            Ok(image::RgbaImage::from_pixel(
                rect.width,
                rect.height,
                image::Rgba([index as u8 + 1, 0, 0, 255]),
            ))
        })
        .unwrap();
        assert_eq!(
            regions,
            vec![
                (
                    0,
                    PhysicalRect {
                        x: 1820,
                        y: 20,
                        width: 100,
                        height: 50
                    }
                ),
                (
                    1,
                    PhysicalRect {
                        x: 0,
                        y: 20,
                        width: 100,
                        height: 50
                    }
                ),
            ]
        );
        assert_eq!(image.dimensions(), (200, 50));
        assert_eq!(image.get_pixel(0, 0)[0], 1);
        assert_eq!(image.get_pixel(100, 0)[0], 2);
    }

    #[test]
    fn unavailable_or_oversized_foreground_never_calls_capture() {
        for rect in [
            PhysicalRect {
                x: 0,
                y: 0,
                width: 0,
                height: 1,
            },
            PhysicalRect {
                x: 0,
                y: 0,
                width: 100_000,
                height: 100_000,
            },
            target().bounds,
        ] {
            assert!(
                compose_foreground(rect, &[], |_, _| panic!("no full-screen fallback")).is_err()
            );
        }
        assert!(
            compose_foreground(target().bounds, &[target().bounds], |_, _| Err(
                "denied".into()
            ))
            .is_err()
        );
    }

    struct FakeTree {
        reads: Vec<usize>,
    }
    impl VisibleTree for FakeTree {
        type Node = usize;
        fn root(&mut self, _: &ForegroundTarget, _: &Budget) -> Option<usize> {
            Some(0)
        }
        fn metadata(&mut self, node: &usize, _: &Budget) -> Option<ControlMetadata> {
            Some(ControlMetadata {
                password: *node == 1,
                offscreen: *node == 2,
                bounds: target().bounds,
            })
        }
        fn children(&mut self, node: &usize, _: usize, _: &Budget) -> Vec<usize> {
            if *node == 0 {
                vec![1, 2, 3]
            } else {
                vec![]
            }
        }
        fn text(&mut self, node: &usize, _: usize, _: &Budget) -> String {
            self.reads.push(*node);
            if *node == 3 {
                "可见正文".repeat(800)
            } else {
                panic!("hidden text read")
            }
        }
    }

    #[test]
    fn password_and_offscreen_nodes_are_filtered_before_text_reads() {
        let mut tree = FakeTree { reads: vec![] };
        let budget = Budget::fresh(
            Arc::new(AtomicU64::new(0)),
            Arc::new(AtomicBool::new(false)),
        );
        let body = collect_visible_text(&mut tree, &target(), &budget);
        assert_eq!(tree.reads, vec![3]);
        assert_eq!(body.chars().count(), TEXT_LIMIT);
    }

    #[test]
    fn stale_or_closed_collection_does_not_read_a_provider() {
        let mut tree = FakeTree { reads: vec![] };
        let epoch = Arc::new(AtomicU64::new(0));
        let closed = Arc::new(AtomicBool::new(false));
        let budget = Budget::fresh(epoch.clone(), closed.clone());
        epoch.fetch_add(1, Ordering::SeqCst);
        assert!(collect_visible_text(&mut tree, &target(), &budget).is_empty());
        closed.store(true, Ordering::SeqCst);
        assert!(collect_visible_text(&mut tree, &target(), &budget).is_empty());
        assert!(tree.reads.is_empty());
    }

    #[test]
    fn traversal_respects_total_controls_and_depth_without_reading_an_entire_tree() {
        struct WideOrDeep {
            wide: bool,
            visited: Vec<usize>,
        }
        impl VisibleTree for WideOrDeep {
            type Node = usize;
            fn root(&mut self, _: &ForegroundTarget, _: &Budget) -> Option<usize> {
                Some(0)
            }
            fn metadata(&mut self, node: &usize, _: &Budget) -> Option<ControlMetadata> {
                self.visited.push(*node);
                Some(ControlMetadata {
                    password: false,
                    offscreen: false,
                    bounds: target().bounds,
                })
            }
            fn children(&mut self, node: &usize, _: usize, _: &Budget) -> Vec<usize> {
                if self.wide {
                    if *node == 0 {
                        (1..1000).collect()
                    } else {
                        vec![]
                    }
                } else {
                    vec![node + 1]
                }
            }
            fn text(&mut self, _: &usize, _: usize, _: &Budget) -> String {
                String::new()
            }
        }
        for (wide, expected) in [(true, 500), (false, 21)] {
            let budget = Budget::fresh(
                Arc::new(AtomicU64::new(0)),
                Arc::new(AtomicBool::new(false)),
            );
            let mut tree = WideOrDeep {
                wide,
                visited: vec![],
            };
            collect_visible_text(&mut tree, &target(), &budget);
            assert_eq!(tree.visited.len(), expected);
        }
    }

    #[test]
    fn busy_collector_has_no_second_job_and_discards_late_text() {
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let collector = Arc::new(
            Collector::spawn(move || {
                move |_: &ForegroundTarget, _: &Budget| {
                    entered_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    "late private body".to_string()
                }
            })
            .unwrap(),
        );
        let job = collector.clone();
        let first = std::thread::spawn(move || job.collect(target()));
        entered_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(collector.collect(target()).status, "busy");
        collector.invalidate();
        release_tx.send(()).unwrap();
        assert!(first.join().unwrap().text.is_empty());
    }

    #[test]
    fn shutdown_does_not_join_a_blocked_native_provider() {
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let collector = Arc::new(
            Collector::spawn(move || {
                move |_: &ForegroundTarget, _: &Budget| {
                    entered_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                    "late body".into()
                }
            })
            .unwrap(),
        );
        let job = collector.clone();
        let first = std::thread::spawn(move || job.collect(target()));
        entered_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        collector.shutdown();
        assert_eq!(collector.collect(target()).status, "closed");
        release_tx.send(()).unwrap();
        assert!(first.join().unwrap().text.is_empty());
    }
}
