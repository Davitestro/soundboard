import os
import threading
import customtkinter as ctk
from tkinter import filedialog
import sounddevice as sd
import numpy as np
from pydub import AudioSegment
import json

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")
SOUNDS_FILE = "sounds.json"

# Добавляем опциональную поддержку системного трея
try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_PYSTRAY = True
except Exception:
    _HAS_PYSTRAY = False
    print("pystray/Pillow not installed. To enable tray icon run: pip install pystray pillow")

class SoundboardApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Soundboard")
        self.geometry("800x480")

        self.sounds = {}
        self.sound_paths = {}  # Имя -> путь
        self.current_playing = False
        self.current_name = None
        self.volume = 1.0
        self._last_mic_chunk = None
        self._playback_pos = 0

        self._load_saved_sounds()

        # Виртуальный кабель (авто)
        self.auto_mic_index = self._find_virtual_mic()

        # Ваш микро (для Input)
        self.user_mic_index = None

        # Интерфейс
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        left_frame = ctk.CTkFrame(self, width=300)
        left_frame.grid(row=0, column=0, sticky="nsw", padx=8, pady=8)
        left_frame.grid_propagate(False)

        right_frame = ctk.CTkFrame(self)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        right_frame.grid_columnconfigure(0, weight=1)

        self.scrollable = ctk.CTkScrollableFrame(left_frame, label_text="Loaded Sounds")
        self.scrollable.pack(fill="both", expand=True, padx=6, pady=6)

        load_btn = ctk.CTkButton(right_frame, text="Load files", command=self.load_files)
        load_btn.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        stop_btn = ctk.CTkButton(right_frame, text="Stop", command=self.stop)
        stop_btn.grid(row=1, column=0, sticky="ew", padx=12, pady=6)

        vol_frame = ctk.CTkFrame(right_frame)
        vol_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=6)
        vol_frame.grid_columnconfigure(1, weight=1)
        vol_label = ctk.CTkLabel(vol_frame, text="Volume")
        vol_label.grid(row=0, column=0, padx=(6, 4))
        self.vol_slider = ctk.CTkSlider(vol_frame, from_=0.0, to=1.0, command=self.change_volume)
        self.vol_slider.set(1.0)
        self.vol_slider.grid(row=0, column=1, sticky="ew", padx=(0, 6))

        dev_frame = ctk.CTkFrame(right_frame)
        dev_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=6)
        dev_frame.grid_columnconfigure(1, weight=1)
        dev_label = ctk.CTkLabel(dev_frame, text="Your Microphone")
        dev_label.grid(row=0, column=0, padx=(6,4))
        self.mic_menu = ctk.CTkOptionMenu(dev_frame, values=["None"], command=self._on_mic_select)
        self.mic_menu.grid(row=0, column=1, sticky="ew", padx=(0,6))
        refresh_btn = ctk.CTkButton(dev_frame, text="Refresh", width=80, command=self._refresh_devices)
        refresh_btn.grid(row=0, column=2, padx=6)

        self.now_label = ctk.CTkLabel(right_frame, text="Now: —")
        self.now_label.grid(row=4, column=0, sticky="w", padx=12, pady=(12, 0))

        self._refresh_devices()
        self._start_mic_stream()
        self._start_playback_stream()

        # Tray support
        self._tray_icon = None
        self._tray_thread = None
        # Перехватываем закрытие окна — будем скрывать вместо выхода
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------- Загрузка файлов -----------
    def load_files(self):
        paths = filedialog.askopenfilenames(title="Select audio files",
                                            filetypes=[("Audio files", "*.wav *.mp3 *.ogg *.flac"), ("All files", "*.*")])
        if not paths:
            return
        for p in paths:
            name = os.path.basename(p)
            display = self._unique_name(name)
            threading.Thread(target=self._load_and_add, args=(p, display), daemon=True).start()
            # сохраняем путь
            self.sound_paths[display] = p
        self._save_sound_paths()

    def _unique_name(self, name):
        base = name
        i = 1
        while name in self.sounds:
            name = f"{os.path.splitext(base)[0]} ({i}){os.path.splitext(base)[1]}"
            i += 1
        return name

    def _load_and_add(self, path, display_name):
            try:
                seg = AudioSegment.from_file(path)
            except Exception as e:
                print(f"Failed to load {path}: {e}")
                # Удаляем путь если не удалось загрузить
                self.sound_paths.pop(display_name, None)
                self._save_sound_paths()
                return
            self.sounds[display_name] = seg
            self.after(0, lambda: self._add_button(display_name))

    def _add_button(self, display_name):
        btn = ctk.CTkButton(self.scrollable, text=display_name, anchor="w",
                             command=lambda n=display_name: self.play_sound(n))
        btn.pack(fill="x", padx=4, pady=4)

    # ----------- Микрофон -----------
    def _start_mic_stream(self):
        if self.user_mic_index is None:
            return
        try:
            self.mic_stream = sd.InputStream(
                device=self.user_mic_index,
                channels=1,
                samplerate=48000,
                callback=self._mic_callback
            )
            self._last_mic_chunk = np.zeros((1024,1), dtype=np.float32)  # инициализация
            self.mic_stream.start()
        except Exception as e:
            print(f"Failed to start mic stream: {e}")
    def _mic_callback(self, indata, frames, time, status):
        self._last_mic_chunk = np.ascontiguousarray(indata, dtype=np.float32)

    def _mic_to_vc(self, indata, frames, time, status):
        self._last_mic_chunk = np.ascontiguousarray(indata, dtype=np.float32)

    def _on_mic_select(self, value):
        if value == "None":
            self.user_mic_index = None
            # Закрываем поток, если был
            if hasattr(self, "mic_stream") and self.mic_stream is not None:
                self.mic_stream.close()
                self.mic_stream = None
            return
        try:
            idx = int(value.split(":")[0])
            self.user_mic_index = idx
            # Перезапуск микрофона
            if hasattr(self, "mic_stream") and self.mic_stream is not None:
                self.mic_stream.close()
            self._start_mic_stream()
        except Exception as e:
            print(f"Failed to select mic: {e}")


    # ----------- Виртуальный микрофон -----------
    def _find_virtual_mic(self):
        try:
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                name = (d.get('name') or "").lower()
                if "cable input" in name or "vb-audio" in name:
                    if d.get('max_output_channels', 0) > 0:
                        print(f"Found virtual mic: {d['name']} (id={i})")
                        return i
        except Exception as e:
            print(f"Find virtual mic error: {e}")
        return None

    # ----------- Микширование и воспроизведение -----------
    def _segment_to_numpy(self, seg: AudioSegment, target_sr: int = 48000):
        if seg.frame_rate != target_sr:
            seg = seg.set_frame_rate(target_sr)
        samples = np.array(seg.get_array_of_samples())
        channels = seg.channels
        if channels > 1:
            samples = samples.reshape((-1, channels))
        sample_width = seg.sample_width
        max_val = float(2 ** (8 * sample_width - 1))
        arr = samples.astype(np.float32) / max_val
        arr = arr * self.volume
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        return arr, target_sr

    def _start_playback_stream(self):
        if self.auto_mic_index is None:
            return
        self.playback_stream = sd.OutputStream(
            device=self.auto_mic_index,
            channels=1,
            samplerate=48000,
            callback=self._playback_callback
        )
        self.playback_stream.start()

    def _playback_callback(self, outdata, frames, time, status):
        # Если сейчас играет звук Soundboard — микрофон не транслируем
        if self.current_playing:
            mic_data = np.zeros((frames,1), dtype=np.float32)
        else:
            # Берём данные микрофона
            if self._last_mic_chunk is None or len(self._last_mic_chunk) < frames:
                mic_data = np.zeros((frames,1), dtype=np.float32)
            else:
                mic_data = self._last_mic_chunk[:frames]

        # Soundboard
        sb_data = np.zeros_like(mic_data)
        if self.current_name and self.current_name in self.sounds:
            arr, sr = self._segment_to_numpy(self.sounds[self.current_name])
            if arr.shape[1] > 1:
                arr = arr.mean(axis=1, keepdims=True)
            start = getattr(self, "_playback_pos", 0)
            end = min(start+frames, len(arr))
            sb_data[:end-start] = arr[start:end]
            self._playback_pos = end
            if end >= len(arr):
                self.current_playing = False
                self.current_name = None
                self.now_label.configure(text="Now: —")

        outdata[:] = np.clip(mic_data + sb_data, -1.0, 1.0)

    def play_sound(self, name):
        if name not in self.sounds:
            return
        self.current_name = name
        self.current_playing = True
        self.now_label.configure(text=f"Now: {name}")
        arr, sr = self._segment_to_numpy(self.sounds[name])
        # Воспроизводим на наушники отдельно
        threading.Thread(target=lambda: sd.play(arr, samplerate=sr), daemon=True).start()
        self._playback_pos = 0

    def stop(self):
        try:
            sd.stop()
        except Exception:
            pass
        self.current_playing = False
        self.current_name = None
        self.now_label.configure(text="Now: —")

    def change_volume(self, value):
        self.volume = float(value)

    # ----------- Обновление устройств -----------
    def _refresh_devices(self):
        try:
            devices = sd.query_devices()
            mic_list = ["None"]
            for i, d in enumerate(devices):
                if d.get('max_input_channels', 0) > 0:
                    mic_list.append(f"{i}: {d['name']}")
            self.mic_menu.configure(values=mic_list)
            # Если текущее значение не в списке, ставим None
            if self.mic_menu.get() not in mic_list:
                self.mic_menu.set("None")
        except Exception as e:
            print(f"Device refresh error: {e}")
    
        # ----------------- JSON save/load -----------------
    def _save_sound_paths(self):
        try:
            with open(SOUNDS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.sound_paths, f, indent=2)
        except Exception as e:
            print(f"Error saving sounds.json: {e}")

    def _load_saved_sounds(self):
        if not os.path.exists(SOUNDS_FILE):
            return
        try:
            with open(SOUNDS_FILE, "r", encoding="utf-8") as f:
                paths = json.load(f)
            for display_name, path in paths.items():
                if os.path.exists(path):
                    threading.Thread(target=self._load_and_add, args=(path, display_name), daemon=True).start()
                else:
                    print(f"File not found, skipping: {path}")
        except Exception as e:
            print(f"Error loading sounds.json: {e}")

    # ----------- Трей (системный лоток) -----------
    def _create_tray_icon(self):
        if not _HAS_PYSTRAY:
            return None
        # простая иконка: круг с буквой S
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=(30, 144, 255, 255))
        # текст может быть несовершенным без шрифта, но обычно проходит
        draw.text((20, 18), "S", fill=(255, 255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda icon, item: self.after(0, self._show_window)),
            pystray.MenuItem("Exit", lambda icon, item: self.after(0, self._exit_app))
        )
        icon = pystray.Icon("soundboard", image, "Soundboard", menu)
        return icon

    def _start_tray(self):
        if not _HAS_PYSTRAY or self._tray_icon is not None:
            return
        self._tray_icon = self._create_tray_icon()
        if not self._tray_icon:
            return
        # Запускаем pystray в отдельном потоке — run блокирует
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _show_window(self):
        # Останавливаем трей-иконку если запущена
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        # Показываем окно
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _on_close(self):
        # Скрываем окно в трей
        try:
            self.withdraw()
        except Exception:
            pass
        if _HAS_PYSTRAY:
            self._start_tray()
        else:
            print("App hidden (no tray support). Install pystray and pillow to enable tray icon.")

    def _exit_app(self):
        # Остановка стримов и очищение трея, затем завершение приложения
        try:
            if hasattr(self, "mic_stream") and self.mic_stream is not None:
                try:
                    self.mic_stream.stop()
                    self.mic_stream.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if hasattr(self, "playback_stream") and self.playback_stream is not None:
                try:
                    self.playback_stream.stop()
                    self.playback_stream.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            sd.stop()
        except Exception:
            pass
        # Удаляем иконку трея
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        # Закрываем GUI
        try:
            self.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    app = SoundboardApp()
    app.mainloop()
