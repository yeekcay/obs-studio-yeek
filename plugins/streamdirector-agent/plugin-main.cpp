#include <obs-module.h>
#include <obs-frontend-api.h>

#include "PythonBridge.hpp"
#include "AgentDock.hpp"
#include "AudioSwitcherDock.hpp"

#include <filesystem>
#include <string>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("streamdirector-agent", "en-US")

static AgentDock *agentDock = nullptr;
static AudioSwitcherDock *audioSwitcherDock = nullptr;

static std::string GetPluginDataPath()
{
	const char *dataPath = obs_module_file("");
	if (dataPath) {
		std::string path(dataPath);
		bfree((void *)dataPath);
		return path;
	}
	return "";
}

static std::string GetPythonHome()
{
	std::string pluginDir = GetPluginDataPath();
	if (pluginDir.empty())
		return "";

	std::string pyHome = pluginDir + "/python";
	if (std::filesystem::exists(pyHome))
		return pyHome;

	return "";
}

static std::string GetAgentModulePath()
{
	std::string pluginDir = GetPluginDataPath();
	if (pluginDir.empty())
		return "";

	/* The Python files (sd_agent_runner.py, sd_direct_api.py) are
	 * installed directly in the plugin data directory. */
	if (std::filesystem::exists(pluginDir + "/sd_agent_runner.py"))
		return pluginDir;

	return "";
}

bool obs_module_load(void)
{
	blog(LOG_INFO, "[streamdirector-agent] Loading StreamDirector Agent plugin");

	auto &py = PythonBridge::Instance();

	std::string pyHome = GetPythonHome();
	if (!pyHome.empty()) {
		py.SetPythonHome(pyHome);
		blog(LOG_INFO, "[streamdirector-agent] Python home: %s", pyHome.c_str());
	} else {
		blog(LOG_WARNING, "[streamdirector-agent] No embedded Python found, using system Python");
	}

	std::string agentPath = GetAgentModulePath();
	if (!agentPath.empty()) {
		py.SetAgentModulePath(agentPath);
		blog(LOG_INFO, "[streamdirector-agent] Agent module path: %s", agentPath.c_str());
	} else {
		blog(LOG_WARNING, "[streamdirector-agent] No agent module path found");
	}

	if (!py.Initialize()) {
		blog(LOG_ERROR, "[streamdirector-agent] Failed to initialize Python");
		return false;
	}

	agentDock = new AgentDock((QWidget *)obs_frontend_get_main_window());
	obs_frontend_add_dock_by_id("StreamDirectorAgent", "StreamDirector Agent", agentDock);
	agentDock->setVisible(true);
	agentDock->raise();

	audioSwitcherDock = new AudioSwitcherDock((QWidget *)obs_frontend_get_main_window());
	obs_frontend_add_dock_by_id("StreamDirectorAudioSwitcher", "Audio Scene Switcher", audioSwitcherDock);
	blog(LOG_INFO, "[streamdirector-agent] Audio Scene Switcher dock registered");

	blog(LOG_INFO, "[streamdirector-agent] Plugin loaded successfully");
	return true;
}

void obs_module_unload(void)
{
	blog(LOG_INFO, "[streamdirector-agent] Unloading plugin");

	if (agentDock) {
		obs_frontend_remove_dock("StreamDirectorAgent");
		delete agentDock;
		agentDock = nullptr;
	}

	if (audioSwitcherDock) {
		obs_frontend_remove_dock("StreamDirectorAudioSwitcher");
		delete audioSwitcherDock;
		audioSwitcherDock = nullptr;
	}

	PythonBridge::Instance().Shutdown();
}
