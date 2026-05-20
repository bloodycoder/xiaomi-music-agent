use rodio::{Decoder, OutputStream, OutputStreamHandle, Sink, Source};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Cursor, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use rand::seq::SliceRandom;
use rand::thread_rng;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Track {
    #[serde(default)]
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    artist: String,
    #[serde(default)]
    url: String,
    #[serde(default)]
    duration_ms: Option<u64>,
    #[serde(default)]
    raw: Option<Value>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum PlayMode {
    Single,
    Sequence,
    LoopAll,
    RepeatOne,
    Shuffle,
}

impl PlayMode {
    fn from_request(play_mode: Option<String>, shuffle: Option<bool>) -> Self {
        if shuffle.unwrap_or(false) {
            return PlayMode::Shuffle;
        }
        match play_mode.unwrap_or_else(|| "loop_all".to_string()).to_ascii_lowercase().as_str() {
            "single" | "stop" => PlayMode::Single,
            "sequence" | "ordered" => PlayMode::Sequence,
            "repeat_one" | "repeat-one" | "one" => PlayMode::RepeatOne,
            "shuffle" | "random" => PlayMode::Shuffle,
            _ => PlayMode::LoopAll,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            PlayMode::Single => "single",
            PlayMode::Sequence => "sequence",
            PlayMode::LoopAll => "loop_all",
            PlayMode::RepeatOne => "repeat_one",
            PlayMode::Shuffle => "shuffle",
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Request {
    PlayUrl {
        url: String,
        track: Option<Value>,
        volume: Option<f32>,
    },
    LoadPlaylist {
        playlist_id: Option<String>,
        playlist_name: Option<String>,
        tracks: Vec<Track>,
        start_index: Option<usize>,
        shuffle: Option<bool>,
        play_mode: Option<String>,
        volume: Option<f32>,
    },
    SetMode { play_mode: String },
    Seek { seconds: Option<f64>, ratio: Option<f64> },
    Next,
    Prev,
    Pause,
    Resume,
    Stop,
    Status,
    Shutdown,
}

#[derive(Debug, Serialize)]
struct Response {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
}

struct PlayerState {
    _stream: OutputStream,
    handle: OutputStreamHandle,
    sink: Option<Sink>,
    playlist_id: Option<String>,
    playlist_name: Option<String>,
    tracks: Vec<Track>,
    order: Vec<usize>,
    index: usize,
    play_mode: PlayMode,
    current_track: Option<Track>,
    current_url: Option<String>,
    started_at: Option<Instant>,
    paused_at: Option<Instant>,
    accumulated_paused: Duration,
    duration: Option<Duration>,
    volume: f32,
    paused: bool,
    running: bool,
    resolver_base: String,
}

impl PlayerState {
    fn new(resolver_base: String) -> Result<Self, String> {
        let (stream, handle) = OutputStream::try_default().map_err(|e| format!("audio output init failed: {e}"))?;
        Ok(Self {
            _stream: stream,
            handle,
            sink: None,
            playlist_id: None,
            playlist_name: None,
            tracks: Vec::new(),
            order: Vec::new(),
            index: 0,
            play_mode: PlayMode::LoopAll,
            current_track: None,
            current_url: None,
            started_at: None,
            paused_at: None,
            accumulated_paused: Duration::ZERO,
            duration: None,
            volume: 0.85,
            paused: false,
            running: false,
            resolver_base,
        })
    }

    fn stop_locked(&mut self) {
        if let Some(sink) = &self.sink {
            sink.stop();
        }
        self.sink = None;
        self.current_track = None;
        self.current_url = None;
        self.started_at = None;
        self.paused_at = None;
        self.accumulated_paused = Duration::ZERO;
        self.duration = None;
        self.running = false;
    }

    fn set_playlist_locked(&mut self, playlist_id: Option<String>, playlist_name: Option<String>, tracks: Vec<Track>, start_index: usize, volume: Option<f32>, play_mode: PlayMode) {
        self.stop_locked();
        self.playlist_id = playlist_id;
        self.playlist_name = playlist_name;
        self.tracks = tracks;
        self.play_mode = play_mode;
        self.order = (0..self.tracks.len()).collect();
        if self.play_mode == PlayMode::Shuffle {
            self.order.shuffle(&mut thread_rng());
            // Try to honor the requested start_index even in shuffle mode.
            if start_index < self.tracks.len() {
                if let Some(pos) = self.order.iter().position(|x| *x == start_index) {
                    self.order.swap(0, pos);
                }
            }
        }
        self.index = if self.tracks.is_empty() { 0 } else { start_index.min(self.tracks.len() - 1) };
        if self.play_mode == PlayMode::Shuffle {
            self.index = 0;
        }
        self.paused = false;
        self.running = false;
        if let Some(v) = volume {
            self.volume = v.clamp(0.0, 2.0);
        }
    }

    fn resolve_track_url(&self, track: &Track) -> Result<String, String> {
        if !track.url.trim().is_empty() {
            return Ok(track.url.clone());
        }
        if track.id.trim().is_empty() {
            return Err("track missing id/url".to_string());
        }
        let base = self.resolver_base.trim_end_matches('/').to_string();
        let url = format!("{base}/song_url?id={}", urlencoding::encode(&track.id));
        let client = reqwest::blocking::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .user_agent("xiaomi-music-agent/ter-music-rust")
            .build()
            .map_err(|e| format!("http client build failed: {e}"))?;
        let resp = client.get(url).send().map_err(|e| format!("resolver request failed: {e}"))?;
        let resp = resp.error_for_status().map_err(|e| format!("resolver http status failed: {e}"))?;
        let data: Value = resp.json().map_err(|e| format!("resolver json failed: {e}"))?;
        let resolved = data.get("url").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        if resolved.is_empty() {
            return Err(format!("resolver returned empty url: {data}"));
        }
        Ok(resolved)
    }

    fn download_audio(&self, url: &str) -> Result<Vec<u8>, String> {
        let client = reqwest::blocking::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(120))
            .user_agent("xiaomi-music-agent/ter-music-rust")
            .build()
            .map_err(|e| format!("http client build failed: {e}"))?;
        let mut last_err = String::new();
        for attempt in 1..=3 {
            match client
                .get(url)
                .send()
                .and_then(|r| r.error_for_status())
                .and_then(|r| r.bytes())
            {
                Ok(b) => return Ok(b.to_vec()),
                Err(e) => {
                    last_err = format!("attempt {attempt}: {e}");
                    thread::sleep(Duration::from_millis(250 * attempt as u64));
                }
            }
        }
        Err(format!("download/read audio bytes failed: {last_err}"))
    }

    fn play_track_locked(&mut self, track: Track) -> Result<Value, String> {
        let resolved_url = self.resolve_track_url(&track)?;
        let bytes = self.download_audio(&resolved_url)?;
        let cursor = Cursor::new(bytes);
        let decoder = Decoder::new(BufReader::new(cursor)).map_err(|e| format!("decode failed: {e}"))?;
        let duration = decoder.total_duration();
        let sink = Sink::try_new(&self.handle).map_err(|e| format!("audio sink failed: {e}"))?;
        sink.set_volume(self.volume);
        sink.append(decoder);
        sink.play();

        self.sink = Some(sink);
        self.current_track = Some(track.clone());
        self.current_url = Some(resolved_url);
        self.started_at = Some(Instant::now());
        self.paused_at = None;
        self.accumulated_paused = Duration::ZERO;
        self.duration = duration;
        self.paused = false;
        self.running = true;
        Ok(self.status_json())
    }

    fn play_index_locked(&mut self, index: usize) -> Result<Value, String> {
        if self.tracks.is_empty() {
            return Err("empty playlist".to_string());
        }
        if self.order.len() != self.tracks.len() {
            self.order = (0..self.tracks.len()).collect();
        }
        let cursor = index % self.tracks.len();
        self.index = cursor;
        let actual = *self.order.get(cursor).unwrap_or(&cursor);
        let track = self.tracks[actual].clone();
        self.play_track_locked(track)
    }

    fn next_locked(&mut self) -> Result<Value, String> {
        if self.tracks.is_empty() {
            return Err("empty playlist".to_string());
        }
        let next = (self.index + 1) % self.tracks.len();
        self.play_index_locked(next)
    }

    fn prev_locked(&mut self) -> Result<Value, String> {
        if self.tracks.is_empty() {
            return Err("empty playlist".to_string());
        }
        let prev = if self.index == 0 { self.tracks.len() - 1 } else { self.index - 1 };
        self.play_index_locked(prev)
    }

    fn finish_locked(&mut self) {
        if let Some(sink) = &self.sink { sink.stop(); }
        self.sink = None;
        self.started_at = None;
        self.paused_at = None;
        self.running = false;
        self.paused = false;
    }

    fn advance_after_finish_locked(&mut self) -> Result<Value, String> {
        if self.tracks.is_empty() {
            self.finish_locked();
            return Ok(self.status_json());
        }
        match self.play_mode {
            PlayMode::Single => {
                self.finish_locked();
                Ok(self.status_json())
            }
            PlayMode::RepeatOne => self.play_index_locked(self.index),
            PlayMode::Sequence => {
                if self.index + 1 >= self.tracks.len() {
                    self.finish_locked();
                    Ok(self.status_json())
                } else {
                    self.play_index_locked(self.index + 1)
                }
            }
            PlayMode::LoopAll | PlayMode::Shuffle => self.next_locked(),
        }
    }

    fn set_mode_locked(&mut self, mode: PlayMode) -> Value {
        self.play_mode = mode;
        if self.play_mode == PlayMode::Shuffle && self.tracks.len() > 1 {
            let current_actual = self.order.get(self.index).copied().unwrap_or(self.index);
            self.order = (0..self.tracks.len()).collect();
            self.order.shuffle(&mut thread_rng());
            if let Some(pos) = self.order.iter().position(|x| *x == current_actual) {
                self.order.swap(0, pos);
            }
            self.index = 0;
        } else if self.order.len() != self.tracks.len() {
            self.order = (0..self.tracks.len()).collect();
        }
        self.status_json()
    }

    fn seek_locked(&mut self, seconds: Option<f64>, ratio: Option<f64>) -> Result<Value, String> {
        let Some(sink) = &self.sink else { return Err("not playing".to_string()); };
        let target = if let Some(sec) = seconds {
            Duration::from_secs_f64(sec.max(0.0))
        } else if let (Some(r), Some(total)) = (ratio, self.duration) {
            Duration::from_secs_f64(total.as_secs_f64() * r.clamp(0.0, 1.0))
        } else {
            return Err("missing seconds or ratio with known duration".to_string());
        };
        sink.try_seek(target).map_err(|e| format!("seek failed: {e}"))?;
        self.accumulated_paused = target;
        if self.paused || sink.is_paused() {
            self.started_at = None;
            self.paused_at = Some(Instant::now());
            self.paused = true;
        } else {
            self.started_at = Some(Instant::now());
            self.paused_at = None;
            self.paused = false;
        }
        Ok(self.status_json())
    }

    fn pause_locked(&mut self) -> Value {
        if let Some(sink) = &self.sink {
            if !sink.is_paused() {
                sink.pause();
                self.paused_at = Some(Instant::now());
                self.paused = true;
            }
        }
        self.status_json()
    }

    fn resume_locked(&mut self) -> Value {
        if let Some(sink) = &self.sink {
            if sink.is_paused() {
                if let Some(t) = self.paused_at.take() {
                    self.accumulated_paused += t.elapsed();
                }
                sink.play();
                self.paused = false;
            }
        }
        self.status_json()
    }

    fn elapsed_secs(&self) -> f64 {
        let base = self.accumulated_paused;
        let elapsed = if self.paused || self.paused_at.is_some() {
            base
        } else if let Some(started) = self.started_at {
            base + started.elapsed()
        } else {
            base
        };
        elapsed.as_secs_f64()
    }

    fn status_json(&self) -> Value {
        let alive = self.sink.as_ref().map(|s| !s.empty()).unwrap_or(false);
        let paused = self.sink.as_ref().map(|s| s.is_paused()).unwrap_or(false) || self.paused;
        let track = self.current_track.as_ref().map(|t| json!({
            "id": t.id,
            "name": t.name,
            "artist": t.artist,
            "duration_ms": t.duration_ms,
            "url": t.url,
        }));
        json!({
            "backend": "ter-music-rust",
            "playlist_engine": true,
            "alive": alive,
            "playing": alive && !paused,
            "paused": paused,
            "finished": self.sink.is_some() && !alive,
            "elapsed": self.elapsed_secs(),
            "duration": self.duration.map(|d| d.as_secs_f64()),
            "volume": self.volume,
            "playlist_id": self.playlist_id,
            "playlist_name": self.playlist_name,
            "index": self.index,
            "play_mode": self.play_mode.as_str(),
            "order": self.order,
            "track": track,
            "url": self.current_url,
            "track_count": self.tracks.len(),
        })
    }
}

fn write_response(stream: &mut UnixStream, resp: Response) {
    let line = serde_json::to_string(&resp).unwrap_or_else(|e| format!(r#"{{"ok":false,"error":"serialize: {e}"}}"#));
    let _ = writeln!(stream, "{line}");
    let _ = stream.flush();
}

fn make_track(value: &Value) -> Track {
    let id = value.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let name = value.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let artist = value.get("artist").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let url = value.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let duration_ms = value.get("duration_ms").and_then(|v| v.as_u64());
    Track { id, name, artist, url, duration_ms, raw: Some(value.clone()) }
}

fn handle_client(mut stream: UnixStream, state: Arc<Mutex<PlayerState>>, shutdown: Arc<Mutex<bool>>) {
    let cloned = match stream.try_clone() {
        Ok(s) => s,
        Err(e) => {
            write_response(&mut stream, Response { ok: false, error: Some(e.to_string()), data: None });
            return;
        }
    };
    let mut reader = BufReader::new(cloned);
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) => return,
        Ok(_) => {}
        Err(e) => {
            write_response(&mut stream, Response { ok: false, error: Some(e.to_string()), data: None });
            return;
        }
    }
    let req: Request = match serde_json::from_str(line.trim()) {
        Ok(r) => r,
        Err(e) => {
            write_response(&mut stream, Response { ok: false, error: Some(format!("bad json: {e}")), data: None });
            return;
        }
    };
    let result = match req {
        Request::PlayUrl { url, track, volume } => {
            let t = track.as_ref().map(make_track).unwrap_or(Track { id: String::new(), name: String::new(), artist: String::new(), url: url.clone(), duration_ms: None, raw: track });
            let mut st = state.lock().unwrap();
            st.stop_locked();
            st.tracks = Vec::new();
            st.order = Vec::new();
            st.index = 0;
            st.play_mode = PlayMode::Single;
            if let Some(v) = volume { st.volume = v.clamp(0.0, 2.0); }
            st.play_track_locked(t)
        }
        Request::LoadPlaylist { playlist_id, playlist_name, tracks, start_index, shuffle, play_mode, volume } => {
            let mut st = state.lock().unwrap();
            let mode = PlayMode::from_request(play_mode, shuffle);
            st.set_playlist_locked(playlist_id, playlist_name, tracks, start_index.unwrap_or(0), volume, mode);
            let idx = st.index;
            st.play_index_locked(idx)
        }
        Request::SetMode { play_mode } => {
            let mut st = state.lock().unwrap();
            let mode = PlayMode::from_request(Some(play_mode), None);
            Ok(st.set_mode_locked(mode))
        }
        Request::Seek { seconds, ratio } => {
            let mut st = state.lock().unwrap();
            st.seek_locked(seconds, ratio)
        }
        Request::Next => {
            let mut st = state.lock().unwrap();
            st.next_locked()
        }
        Request::Prev => {
            let mut st = state.lock().unwrap();
            st.prev_locked()
        }
        Request::Pause => Ok(state.lock().unwrap().pause_locked()),
        Request::Resume => Ok(state.lock().unwrap().resume_locked()),
        Request::Stop => {
            let mut st = state.lock().unwrap();
            st.stop_locked();
            st.paused = false;
            Ok(st.status_json())
        }
        Request::Status => Ok(state.lock().unwrap().status_json()),
        Request::Shutdown => {
            let mut st = state.lock().unwrap();
            st.stop_locked();
            *shutdown.lock().unwrap() = true;
            Ok(json!({"shutdown": true}))
        }
    };
    match result {
        Ok(data) => write_response(&mut stream, Response { ok: true, error: None, data: Some(data) }),
        Err(e) => write_response(&mut stream, Response { ok: false, error: Some(e), data: None }),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let socket_path = args.windows(2)
        .find(|w| w[0] == "--socket")
        .map(|w| w[1].clone())
        .unwrap_or_else(|| "/tmp/xiaomi-music-ter-player.sock".to_string());
    let resolver_base = args.windows(2)
        .find(|w| w[0] == "--resolver")
        .map(|w| w[1].clone())
        .or_else(|| std::env::var("MUSIC_AGENT_URL").ok())
        .unwrap_or_else(|| "http://127.0.0.1:8765".to_string());

    if Path::new(&socket_path).exists() {
        let _ = fs::remove_file(&socket_path);
    }
    let listener = UnixListener::bind(&socket_path).unwrap_or_else(|e| panic!("bind {socket_path}: {e}"));
    eprintln!("agent_player listening on {socket_path}");

    listener.set_nonblocking(true).unwrap_or_else(|e| panic!("set_nonblocking: {e}"));
    let state = Arc::new(Mutex::new(PlayerState::new(resolver_base).unwrap_or_else(|e| panic!("{e}"))));
    let shutdown = Arc::new(Mutex::new(false));

    loop {
        if *shutdown.lock().unwrap() {
            break;
        }
        match listener.accept() {
            Ok((stream, _addr)) => {
                let _ = stream.set_nonblocking(false);
                handle_client(stream, state.clone(), shutdown.clone())
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(e) => eprintln!("accept failed: {e}"),
        }

        {
            let mut st = state.lock().unwrap();
            if st.sink.is_some() && !st.paused && !st.tracks.is_empty() {
                let finished = st.sink.as_ref().map(|s| s.empty()).unwrap_or(false);
                if finished {
                    let _ = st.advance_after_finish_locked();
                }
            }
        }

        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = fs::remove_file(&socket_path);
}
