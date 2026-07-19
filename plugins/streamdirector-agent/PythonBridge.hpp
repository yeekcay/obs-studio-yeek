#pragma once

#include <string>
#include <functional>
#include <atomic>
#include <mutex>

struct _ts;
using PyThreadState = _ts;

class PythonBridge {
public:
	static PythonBridge &Instance();

	bool Initialize();
	void Shutdown();

	bool IsInitialized() const { return initialized; }

	void SetPythonHome(const std::string &path);
	void SetAgentModulePath(const std::string &path);

	bool RunAgent(const std::string &instruction, std::string &result, const std::string &mode = "responsive");
	bool IsAgentRunning() const { return agentRunning; }
	bool IsStopRequested() const { return stopRequested; }
	void StopAgent();

	void SetOutputCallback(std::function<void(const std::string &)> callback);
	void EmitLog(const std::string &text);

	/* Audio level access for AudioSwitcherDock */
	static float GetSourceMagnitudeDb(const std::string &sourceName);
	static void EnsureAllVolmeters();
	static bool EnsureSourceInScene(const std::string &sourceName, const std::string &sceneName);

private:
	PythonBridge() = default;
	~PythonBridge() = default;
	PythonBridge(const PythonBridge &) = delete;
	PythonBridge &operator=(const PythonBridge &) = delete;

	bool initialized = false;
	std::atomic<bool> agentRunning{false};
	std::atomic<bool> stopRequested{false};
	std::string pythonHome;
	std::string agentModulePath;
	std::function<void(const std::string &)> outputCallback;
	std::mutex callbackMutex;
	PyThreadState *mainThreadState = nullptr;

	void LogOutput(const std::string &text);
};
