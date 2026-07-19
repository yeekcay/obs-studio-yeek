#include "PythonBridge.hpp"

#include <obs-module.h>
#include <obs-frontend-api.h>
#include <obs-audio-controls.h>
#include <map>
#include <string>
#include <mutex>

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <filesystem>

/* --- Audio level monitoring --- */

struct AudioLevels {
	float peak[MAX_AUDIO_CHANNELS] = {};
	float magnitude[MAX_AUDIO_CHANNELS] = {};
	float input_peak[MAX_AUDIO_CHANNELS] = {};
	int nr_channels = 0;
};

static std::map<std::string, AudioLevels> g_audioLevels;
static std::mutex g_audioLevelsMutex;
static std::map<std::string, obs_volmeter_t *> g_volmeters;

static void volmeter_callback(void *param, const float magnitude[MAX_AUDIO_CHANNELS],
			      const float peak[MAX_AUDIO_CHANNELS],
			      const float input_peak[MAX_AUDIO_CHANNELS])
{
	const char *name = (const char *)param;
	if (!name)
		return;

	std::lock_guard<std::mutex> lock(g_audioLevelsMutex);
	AudioLevels &levels = g_audioLevels[name];
	memcpy(levels.peak, peak, sizeof(float) * MAX_AUDIO_CHANNELS);
	memcpy(levels.magnitude, magnitude, sizeof(float) * MAX_AUDIO_CHANNELS);
	memcpy(levels.input_peak, input_peak, sizeof(float) * MAX_AUDIO_CHANNELS);
}

static void ensure_volmeter_for_source(const char *name, obs_source_t *source)
{
	if (g_volmeters.count(name))
		return;

	obs_volmeter_t *vm = obs_volmeter_create(OBS_FADER_CUBIC);
	if (!vm)
		return;

	obs_volmeter_attach_source(vm, source);
	obs_volmeter_add_callback(vm, volmeter_callback, (void *)strdup(name));
	g_volmeters[name] = vm;
	g_audioLevels[name] = AudioLevels{};
}

PythonBridge &PythonBridge::Instance()
{
	static PythonBridge instance;
	return instance;
}

void PythonBridge::SetPythonHome(const std::string &path)
{
	pythonHome = path;
}

void PythonBridge::SetAgentModulePath(const std::string &path)
{
	agentModulePath = path;
}

void PythonBridge::SetOutputCallback(std::function<void(const std::string &)> callback)
{
	std::lock_guard<std::mutex> lock(callbackMutex);
	outputCallback = callback;
}

void PythonBridge::EmitLog(const std::string &text)
{
	LogOutput(text);
}

void PythonBridge::LogOutput(const std::string &text)
{
	blog(LOG_INFO, "[streamdirector-agent] %s", text.c_str());
	std::lock_guard<std::mutex> lock(callbackMutex);
	if (outputCallback)
		outputCallback(text);
}

static PyObject *sd_log(PyObject *self, PyObject *args)
{
	const char *text;
	if (!PyArg_ParseTuple(args, "s", &text))
		return nullptr;
	blog(LOG_INFO, "[streamdirector-agent] %s", text);
	PythonBridge::Instance().EmitLog(text);
	Py_RETURN_NONE;
}

/* --- Scene Management --- */

static PyObject *sd_get_scene_names(PyObject *self, PyObject *args)
{
	char **names = obs_frontend_get_scene_names();
	if (!names) {
		Py_RETURN_NONE;
	}

	PyObject *list = PyList_New(0);
	char **cur = names;
	while (*cur) {
		PyList_Append(list, PyUnicode_FromString(*cur));
		cur++;
	}
	bfree(names);
	return list;
}

static PyObject *sd_get_current_scene(PyObject *self, PyObject *args)
{
	obs_source_t *scene = obs_frontend_get_current_scene();
	if (!scene) {
		Py_RETURN_NONE;
	}
	const char *name = obs_source_get_name(scene);
	PyObject *result = PyUnicode_FromString(name ? name : "");
	obs_source_release(scene);
	return result;
}

static PyObject *sd_set_current_scene(PyObject *self, PyObject *args)
{
	const char *name;
	if (!PyArg_ParseTuple(args, "s", &name))
		return nullptr;

	obs_frontend_source_list scenes = {};
	obs_frontend_get_scenes(&scenes);
	bool found = false;
	for (size_t i = 0; i < scenes.sources.num; i++) {
		const char *sname = obs_source_get_name(scenes.sources.array[i]);
		if (sname && strcmp(sname, name) == 0) {
			obs_frontend_set_current_scene(scenes.sources.array[i]);
			found = true;
			break;
		}
	}
	obs_frontend_source_list_free(&scenes);

	PyObject *result = found ? Py_True : Py_False;
	Py_INCREF(result);
	return result;
}

static PyObject *sd_get_preview_scene(PyObject *self, PyObject *args)
{
	obs_source_t *scene = obs_frontend_get_current_preview_scene();
	if (!scene) {
		Py_RETURN_NONE;
	}
	const char *name = obs_source_get_name(scene);
	PyObject *result = PyUnicode_FromString(name ? name : "");
	obs_source_release(scene);
	return result;
}

static PyObject *sd_is_studio_mode(PyObject *self, PyObject *args)
{
	if (obs_frontend_preview_enabled()) {
		Py_RETURN_TRUE;
	}
	Py_RETURN_FALSE;
}

static PyObject *sd_get_scene_audio_sources(PyObject *self, PyObject *args)
{
	const char *scene_name;
	if (!PyArg_ParseTuple(args, "s", &scene_name))
		return nullptr;

	obs_source_t *scene_source = obs_get_source_by_name(scene_name);
	if (!scene_source) {
		Py_RETURN_NONE;
	}

	obs_scene_t *scene = obs_scene_from_source(scene_source);
	if (!scene) {
		obs_source_release(scene_source);
		Py_RETURN_NONE;
	}

	PyObject *list = PyList_New(0);

	obs_scene_enum_items(
		scene,
		[](obs_scene_t *, obs_sceneitem_t *item, void *param) {
			obs_source_t *source = obs_sceneitem_get_source(item);
			if (!source)
				return true;

			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_AUDIO) {
				const char *name = obs_source_get_name(source);
				if (name) {
					PyList_Append((PyObject *)param, PyUnicode_FromString(name));
				}
			}
			return true;
		},
		list);

	obs_source_release(scene_source);
	return list;
}

/* --- Streaming --- */

static PyObject *sd_start_streaming(PyObject *self, PyObject *args)
{
	obs_frontend_streaming_start();
	Py_RETURN_NONE;
}

static PyObject *sd_stop_streaming(PyObject *self, PyObject *args)
{
	obs_frontend_streaming_stop();
	Py_RETURN_NONE;
}

static PyObject *sd_is_streaming(PyObject *self, PyObject *args)
{
	bool active = obs_frontend_streaming_active();
	if (active) {
		Py_RETURN_TRUE;
	}
	Py_RETURN_FALSE;
}

/* --- Recording --- */

static PyObject *sd_start_recording(PyObject *self, PyObject *args)
{
	obs_frontend_recording_start();
	Py_RETURN_NONE;
}

static PyObject *sd_stop_recording(PyObject *self, PyObject *args)
{
	obs_frontend_recording_stop();
	Py_RETURN_NONE;
}

static PyObject *sd_is_recording(PyObject *self, PyObject *args)
{
	bool active = obs_frontend_recording_active();
	if (active) {
		Py_RETURN_TRUE;
	}
	Py_RETURN_FALSE;
}

static PyObject *sd_pause_recording(PyObject *self, PyObject *args)
{
	obs_frontend_recording_pause(true);
	Py_RETURN_NONE;
}

static PyObject *sd_resume_recording(PyObject *self, PyObject *args)
{
	obs_frontend_recording_pause(false);
	Py_RETURN_NONE;
}

/* --- Audio --- */

static PyObject *sd_get_source_volume(PyObject *self, PyObject *args)
{
	const char *name;
	if (!PyArg_ParseTuple(args, "s", &name))
		return nullptr;

	obs_source_t *source = obs_get_source_by_name(name);
	if (!source) {
		Py_RETURN_NONE;
	}

	float vol = obs_source_get_volume(source);
	bool muted = obs_source_muted(source);
	obs_source_release(source);

	PyObject *dict = PyDict_New();
	PyDict_SetItemString(dict, "volume", PyFloat_FromDouble(vol));
	PyDict_SetItemString(dict, "muted", muted ? Py_True : Py_False);
	return dict;
}

static PyObject *sd_set_source_volume(PyObject *self, PyObject *args)
{
	const char *name;
	float volume;
	if (!PyArg_ParseTuple(args, "sf", &name, &volume))
		return nullptr;

	obs_source_t *source = obs_get_source_by_name(name);
	if (!source) {
		Py_RETURN_FALSE;
	}

	obs_source_set_volume(source, volume);
	obs_source_release(source);
	Py_RETURN_TRUE;
}

static PyObject *sd_set_source_muted(PyObject *self, PyObject *args)
{
	const char *name;
	int muted;
	if (!PyArg_ParseTuple(args, "sp", &name, &muted))
		return nullptr;

	obs_source_t *source = obs_get_source_by_name(name);
	if (!source) {
		Py_RETURN_FALSE;
	}

	obs_source_set_muted(source, muted ? true : false);
	obs_source_release(source);
	Py_RETURN_TRUE;
}

static PyObject *sd_get_audio_sources(PyObject *self, PyObject *args)
{
	PyObject *list = PyList_New(0);

	obs_enum_sources(
		[](void *param, obs_source_t *source) {
			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_AUDIO) {
				const char *name = obs_source_get_name(source);
				if (name) {
					PyList_Append((PyObject *)param, PyUnicode_FromString(name));
				}
			}
			return true;
		},
		list);

	return list;
}

static PyObject *sd_get_audio_levels(PyObject *self, PyObject *args)
{
	PyObject *list = PyList_New(0);

	obs_enum_sources(
		[](void *param, obs_source_t *source) {
			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_AUDIO) {
				const char *name = obs_source_get_name(source);
				if (!name)
					return true;

				ensure_volmeter_for_source(name, source);
			}
			return true;
		},
		nullptr);

	std::lock_guard<std::mutex> lock(g_audioLevelsMutex);
	for (auto &[name, levels] : g_audioLevels) {
		PyObject *dict = PyDict_New();
		PyDict_SetItemString(dict, "name", PyUnicode_FromString(name.c_str()));

		float max_peak = -INFINITY;
		float max_magnitude = -INFINITY;
		float max_input_peak = -INFINITY;
		for (int i = 0; i < MAX_AUDIO_CHANNELS; i++) {
			if (levels.peak[i] > max_peak)
				max_peak = levels.peak[i];
			if (levels.magnitude[i] > max_magnitude)
				max_magnitude = levels.magnitude[i];
			if (levels.input_peak[i] > max_input_peak)
				max_input_peak = levels.input_peak[i];
		}

		PyDict_SetItemString(dict, "peak_db", PyFloat_FromDouble(max_peak));
		PyDict_SetItemString(dict, "magnitude_db", PyFloat_FromDouble(max_magnitude));
		PyDict_SetItemString(dict, "input_peak_db", PyFloat_FromDouble(max_input_peak));

		PyList_Append(list, dict);
	}

	return list;
}

static PyObject *sd_get_stats(PyObject *self, PyObject *args)
{
	obs_output_t *stream_output = obs_frontend_get_streaming_output();
	obs_output_t *record_output = obs_frontend_get_recording_output();

	PyObject *dict = PyDict_New();

	uint64_t total_frames = 0;
	uint64_t skipped_frames = 0;

	if (stream_output && obs_output_active(stream_output)) {
		total_frames = obs_output_get_total_frames(stream_output);
		skipped_frames = obs_output_get_frames_dropped(stream_output);
	}

	PyDict_SetItemString(dict, "streaming_total_frames", PyLong_FromUnsignedLongLong(total_frames));
	PyDict_SetItemString(dict, "streaming_skipped_frames", PyLong_FromUnsignedLongLong(skipped_frames));

	if (stream_output)
		obs_output_release(stream_output);
	if (record_output)
		obs_output_release(record_output);

	return dict;
}

static PyMethodDef SDMethods[] = {
	{"log", sd_log, METH_VARARGS, "Log a message to StreamDirector"},
	{"get_scene_names", sd_get_scene_names, METH_NOARGS, "Get list of scene names"},
	{"get_current_scene", sd_get_current_scene, METH_NOARGS, "Get current scene name"},
	{"set_current_scene", sd_set_current_scene, METH_VARARGS, "Switch to scene by name"},
	{"get_preview_scene", sd_get_preview_scene, METH_NOARGS, "Get Studio Mode preview scene name"},
	{"is_studio_mode", sd_is_studio_mode, METH_NOARGS, "Check if Studio Mode is enabled"},
	{"get_scene_audio_sources", sd_get_scene_audio_sources, METH_VARARGS, "Get audio source names within a specific scene"},
	{"start_streaming", sd_start_streaming, METH_NOARGS, "Start streaming"},
	{"stop_streaming", sd_stop_streaming, METH_NOARGS, "Stop streaming"},
	{"is_streaming", sd_is_streaming, METH_NOARGS, "Check if streaming is active"},
	{"start_recording", sd_start_recording, METH_NOARGS, "Start recording"},
	{"stop_recording", sd_stop_recording, METH_NOARGS, "Stop recording"},
	{"is_recording", sd_is_recording, METH_NOARGS, "Check if recording is active"},
	{"pause_recording", sd_pause_recording, METH_NOARGS, "Pause recording"},
	{"resume_recording", sd_resume_recording, METH_NOARGS, "Resume recording"},
	{"get_source_volume", sd_get_source_volume, METH_VARARGS, "Get volume and mute state for a source"},
	{"set_source_volume", sd_set_source_volume, METH_VARARGS, "Set volume for a source"},
	{"set_source_muted", sd_set_source_muted, METH_VARARGS, "Set mute state for a source"},
	{"get_audio_sources", sd_get_audio_sources, METH_NOARGS, "Get list of audio source names"},
	{"get_audio_levels", sd_get_audio_levels, METH_NOARGS, "Get real-time audio levels (peak/magnitude dB) for all audio sources"},
	{"get_stats", sd_get_stats, METH_NOARGS, "Get streaming/recording stats"},
	{nullptr, nullptr, 0, nullptr},
};

static PyModuleDef SDModule = {
	PyModuleDef_HEAD_INIT,
	"_streamdirector",
	"StreamDirector internal bridge module",
	-1,
	SDMethods,
	nullptr,
	nullptr,
	nullptr,
	nullptr,
};

static PyObject *PyInit_streamdirector(void)
{
	return PyModule_Create(&SDModule);
}

bool PythonBridge::Initialize()
{
	if (initialized)
		return true;

	if (!Py_IsInitialized()) {
		if (!pythonHome.empty()) {
			PyConfig config;
			PyConfig_InitPythonConfig(&config);
			config.parse_argv = 0;
			config.install_signal_handlers = 0;

			wchar_t *wpath = Py_DecodeLocale(pythonHome.c_str(), nullptr);
			if (wpath) {
				PyConfig_SetString(&config, &config.home, wpath);
				if (Py_IsInitialized()) {
					PyConfig_Clear(&config);
				} else {
					Py_InitializeFromConfig(&config);
					PyConfig_Clear(&config);
				}
				PyMem_RawFree(wpath);
			}

			if (!Py_IsInitialized()) {
				PyImport_AppendInittab("_streamdirector", PyInit_streamdirector);
				Py_Initialize();
			}
		} else {
			PyImport_AppendInittab("_streamdirector", PyInit_streamdirector);
			Py_Initialize();
		}
	}

	if (!agentModulePath.empty()) {
		blog(LOG_INFO, "[streamdirector-agent] Agent module path: %s", agentModulePath.c_str());
		PyObject *sysPath = PySys_GetObject("path");
		PyObject *pPath = PyUnicode_FromString(agentModulePath.c_str());
		PyList_Insert(sysPath, 0, pPath);
		Py_DECREF(pPath);

		/* Log sys.path for debugging */
		PyRun_SimpleString(
			"import sys\n"
			"import _streamdirector\n"
			"_streamdirector.log('Python sys.path:')\n"
			"for p in sys.path:\n"
			"    _streamdirector.log('  ' + str(p))\n"
		);
	} else {
		blog(LOG_WARNING, "[streamdirector-agent] Agent module path is empty!");
	}

	/* Redirect Python stdout/stderr to our log callback */
	PyRun_SimpleString(
		"import sys\n"
		"import _streamdirector\n"
		"class _SDStream:\n"
		"    def __init__(self, prefix=''):\n"
		"        self.prefix = prefix\n"
		"        self._buf = ''\n"
		"    def write(self, text):\n"
		"        self._buf += text\n"
		"        while '\\n' in self._buf:\n"
		"            line, self._buf = self._buf.split('\\n', 1)\n"
		"            if line.strip():\n"
		"                _streamdirector.log(self.prefix + line)\n"
		"    def flush(self):\n"
		"        if self._buf.strip():\n"
		"            _streamdirector.log(self.prefix + self._buf)\n"
		"            self._buf = ''\n"
		"sys.stdout = _SDStream('[py] ')\n"
		"sys.stderr = _SDStream('[py-err] ')\n"
	);

	initialized = true;
	blog(LOG_INFO, "[streamdirector-agent] Python initialized successfully");

	/* Release the GIL so worker threads can acquire it via PyGILState_Ensure */
	mainThreadState = PyEval_SaveThread();
	blog(LOG_INFO, "[streamdirector-agent] GIL released after initialization");
	return true;
}

void PythonBridge::Shutdown()
{
	if (!initialized)
		return;

	/* Re-acquire the GIL before finalizing Python */
	if (mainThreadState) {
		PyEval_RestoreThread(mainThreadState);
		mainThreadState = nullptr;
	}

	if (Py_IsInitialized()) {
		Py_FinalizeEx();
	}
	initialized = false;
	blog(LOG_INFO, "[streamdirector-agent] Python shut down");
}

bool PythonBridge::RunAgent(const std::string &instruction, std::string &result, const std::string &mode)
{
	if (!initialized) {
		result = "Python not initialized";
		return false;
	}

	agentRunning = true;

	blog(LOG_INFO, "[streamdirector-agent] RunAgent: acquiring GIL (mode=%s)...", mode.c_str());
	PyGILState_STATE gstate = PyGILState_Ensure();
	blog(LOG_INFO, "[streamdirector-agent] RunAgent: GIL acquired.");

	bool success = false;

	blog(LOG_INFO, "[streamdirector-agent] RunAgent: importing sd_agent_runner...");
	PyObject *pModule = PyImport_ImportModule("sd_agent_runner");
	if (!pModule) {
		blog(LOG_ERROR, "[streamdirector-agent] RunAgent: import failed!");
		PyErr_Print();
		result = "Failed to import sd_agent_runner module. Check that sd_agent_runner.py is in the plugin data directory.";
	} else {
		blog(LOG_INFO, "[streamdirector-agent] RunAgent: module imported, getting run_agent function...");
		PyObject *pFunc = PyObject_GetAttrString(pModule, "run_agent");
		if (!pFunc || !PyCallable_Check(pFunc)) {
			blog(LOG_ERROR, "[streamdirector-agent] RunAgent: run_agent function not found!");
			PyErr_Print();
			result = "Failed to find run_agent function";
		} else {
			blog(LOG_INFO, "[streamdirector-agent] RunAgent: calling run_agent...");
			PyObject *pArgs = PyTuple_Pack(2,
				PyUnicode_FromString(instruction.c_str()),
				PyUnicode_FromString(mode.c_str()));
			PyObject *pValue = PyObject_CallObject(pFunc, pArgs);
			Py_DECREF(pArgs);

			if (pValue) {
				blog(LOG_INFO, "[streamdirector-agent] RunAgent: call returned, converting result...");
				const char *cResult = PyUnicode_AsUTF8(pValue);
				if (cResult) {
					result = cResult;
					success = true;
				} else {
					result = "Failed to convert result to string";
				}
				Py_DECREF(pValue);
			} else {
				blog(LOG_ERROR, "[streamdirector-agent] RunAgent: call failed!");
				PyErr_Print();
				result = "Agent execution failed";
			}
			Py_DECREF(pFunc);
		}
		Py_DECREF(pModule);
	}

	PyGILState_Release(gstate);
	agentRunning = false;

	blog(LOG_INFO, "[streamdirector-agent] RunAgent: done. result=%s", result.c_str());
	LogOutput(result);
	return success;
}

void PythonBridge::StopAgent()
{
	if (!agentRunning)
		return;

	blog(LOG_INFO, "[streamdirector-agent] StopAgent: sending KeyboardInterrupt to Python...");
	PyErr_SetInterrupt();
}
