#include "AudioSwitcherDock.hpp"
#include "PythonBridge.hpp"

#include <obs-module.h>
#include <obs-frontend-api.h>
#include <obs-audio-controls.h>
#include <graphics/graphics.h>

#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QFrame>
#include <QScrollBar>
#include <QScrollArea>
#include <QDateTime>
#include <cmath>
#include <cstring>

#define VIDEO_SAMPLE_W 32
#define VIDEO_SAMPLE_H 32
#define VIDEO_FRAME_SIZE (VIDEO_SAMPLE_W * VIDEO_SAMPLE_H * 4)

AudioSwitcherDock::AudioSwitcherDock(QWidget *parent)
	: QDockWidget("Audio Scene Switcher", parent),
	  thresholdDb_(3.0f),
	  sustainSecs_(2.0f),
	  aDominantSince_(0),
	  bDominantSince_(0),
	  switchCount_(0),
	  running_(false),
	  motionSensitivity_(15.0f),
	  motionSustainSecs_(2.0f),
	  motionDominantSince_(0),
	  lastMotionLevel_(0),
	  videoResourcesReady_(false),
	  videoTexrender_(nullptr),
	  videoStaging_(nullptr),
	  prevFrame_(nullptr),
	  hasPrevFrame_(false),
	  bitrateThresholdKbps_(2000),
	  lastTotalBytes_(0),
	  lastBitrateKbps_(0),
	  lowBitrateSince_(0),
	  streamQualitySwitched_(false)
{
	setObjectName("AudioSceneSwitcher");
	setAllowedAreas(Qt::AllDockWidgetAreas);
	setTitleBarWidget(new QWidget());

	QWidget *container = new QWidget(this);
	QVBoxLayout *outerLayout = new QVBoxLayout(container);
	outerLayout->setContentsMargins(0, 0, 0, 0);

	QScrollArea *scrollArea = new QScrollArea(container);
	scrollArea->setWidgetResizable(true);
	scrollArea->setFrameShape(QFrame::NoFrame);
	scrollArea->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

	QWidget *content = new QWidget();
	scrollArea->setWidget(content);
	outerLayout->addWidget(scrollArea);

	setWidget(container);

	QVBoxLayout *mainLayout = new QVBoxLayout(content);
	mainLayout->setContentsMargins(6, 6, 6, 6);

	/* --- Scene Selection --- */
	QGroupBox *sceneGroup = new QGroupBox("Scene Selection", content);
	QGridLayout *sceneLayout = new QGridLayout(sceneGroup);
	sceneLayout->setColumnStretch(1, 1);

	sceneLayout->addWidget(new QLabel("Scene A:", sceneGroup), 0, 0);
	sceneACombo_ = new QComboBox(sceneGroup);
	sceneLayout->addWidget(sceneACombo_, 0, 1);

	sceneLayout->addWidget(new QLabel("Scene B:", sceneGroup), 1, 0);
	sceneBCombo_ = new QComboBox(sceneGroup);
	sceneLayout->addWidget(sceneBCombo_, 1, 1);

	mainLayout->addWidget(sceneGroup);

	/* --- Source Selection --- */
	QGroupBox *sourceGroup = new QGroupBox("Audio Sources to Monitor", content);
	QGridLayout *sourceLayout = new QGridLayout(sourceGroup);
	sourceLayout->setColumnStretch(1, 1);

	sourceLayout->addWidget(new QLabel("Source A:", sourceGroup), 0, 0);
	sourceACombo_ = new QComboBox(sourceGroup);
	sourceLayout->addWidget(sourceACombo_, 0, 1);

	sourceLayout->addWidget(new QLabel("Source B:", sourceGroup), 1, 0);
	sourceBCombo_ = new QComboBox(sourceGroup);
	sourceLayout->addWidget(sourceBCombo_, 1, 1);

	mainLayout->addWidget(sourceGroup);

	/* --- Thresholds --- */
	QGroupBox *threshGroup = new QGroupBox("Switching Thresholds", content);
	QGridLayout *threshLayout = new QGridLayout(threshGroup);

	thresholdLabel_ = new QLabel("3.0 dB", threshGroup);
	thresholdSlider_ = new QSlider(Qt::Horizontal, threshGroup);
	thresholdSlider_->setRange(1, 20);
	thresholdSlider_->setValue(3);
	thresholdSlider_->setSingleStep(1);
	threshLayout->addWidget(new QLabel("Volume difference:", threshGroup), 0, 0);
	threshLayout->addWidget(thresholdSlider_, 0, 1);
	threshLayout->addWidget(thresholdLabel_, 0, 2);

	sustainLabel_ = new QLabel("2.0 s", threshGroup);
	sustainSlider_ = new QSlider(Qt::Horizontal, threshGroup);
	sustainSlider_->setRange(1, 10);
	sustainSlider_->setValue(2);
	sustainSlider_->setSingleStep(1);
	threshLayout->addWidget(new QLabel("Sustain time:", threshGroup), 1, 0);
	threshLayout->addWidget(sustainSlider_, 1, 1);
	threshLayout->addWidget(sustainLabel_, 1, 2);

	mainLayout->addWidget(threshGroup);

	/* --- Live Level Display --- */
	QGroupBox *levelGroup = new QGroupBox("Live Audio Levels", content);
	QGridLayout *levelLayout = new QGridLayout(levelGroup);

	levelALabel_ = new QLabel("Source A: --.- dB", levelGroup);
	levelALabel_->setStyleSheet("font-family: monospace; font-size: 14px; padding: 4px;");
	levelBLabel_ = new QLabel("Source B: --.- dB", levelGroup);
	levelBLabel_->setStyleSheet("font-family: monospace; font-size: 14px; padding: 4px;");

	levelLayout->addWidget(levelALabel_, 0, 0);
	levelLayout->addWidget(levelBLabel_, 1, 0);

	mainLayout->addWidget(levelGroup);

	/* --- Controls --- */
	QHBoxLayout *ctrlLayout = new QHBoxLayout();

	startButton_ = new QPushButton("Start", content);
	stopButton_ = new QPushButton("Stop", content);
	stopButton_->setEnabled(false);
	refreshButton_ = new QPushButton("Refresh", content);

	ctrlLayout->addWidget(startButton_);
	ctrlLayout->addWidget(stopButton_);
	ctrlLayout->addWidget(refreshButton_);
	ctrlLayout->addStretch();

	mainLayout->addLayout(ctrlLayout);

	/* --- Status --- */
	QHBoxLayout *statusLayout = new QHBoxLayout();
	statusLabel_ = new QLabel("Status: Idle", content);
	statusLabel_->setStyleSheet("padding: 4px; color: #888;");
	switchCountLabel_ = new QLabel("Switches: 0", content);
	switchCountLabel_->setStyleSheet("padding: 4px; color: #888;");
	statusLayout->addWidget(statusLabel_);
	statusLayout->addStretch();
	statusLayout->addWidget(switchCountLabel_);

	mainLayout->addLayout(statusLayout);

	/* --- Video Motion Detection (Optional) --- */
	QGroupBox *videoGroup = new QGroupBox("Video Motion Detection (Optional)", content);
	QVBoxLayout *videoLayout = new QVBoxLayout(videoGroup);

	videoEnableCheck_ = new QCheckBox("Enable motion-triggered scene switching", videoGroup);
	videoLayout->addWidget(videoEnableCheck_);

	QGridLayout *videoGrid = new QGridLayout();
	videoGrid->setColumnStretch(1, 1);
	videoGrid->addWidget(new QLabel("Monitor source:", videoGroup), 0, 0);
	videoSourceCombo_ = new QComboBox(videoGroup);
	videoGrid->addWidget(videoSourceCombo_, 0, 1);

	videoGrid->addWidget(new QLabel("Switch to scene:", videoGroup), 1, 0);
	videoSceneCombo_ = new QComboBox(videoGroup);
	videoGrid->addWidget(videoSceneCombo_, 1, 1);

	motionSensLabel_ = new QLabel("15 %", videoGroup);
	motionSensSlider_ = new QSlider(Qt::Horizontal, videoGroup);
	motionSensSlider_->setRange(1, 80);
	motionSensSlider_->setValue(15);
	videoGrid->addWidget(new QLabel("Motion sensitivity:", videoGroup), 2, 0);
	videoGrid->addWidget(motionSensSlider_, 2, 1);
	videoGrid->addWidget(motionSensLabel_, 2, 2);

	motionSustainLabel_ = new QLabel("2.0 s", videoGroup);
	motionSustainSlider_ = new QSlider(Qt::Horizontal, videoGroup);
	motionSustainSlider_->setRange(1, 10);
	motionSustainSlider_->setValue(2);
	videoGrid->addWidget(new QLabel("Sustain time:", videoGroup), 3, 0);
	videoGrid->addWidget(motionSustainSlider_, 3, 1);
	videoGrid->addWidget(motionSustainLabel_, 3, 2);

	videoLayout->addLayout(videoGrid);

	motionLevelLabel_ = new QLabel("Motion: ---%", videoGroup);
	motionLevelLabel_->setStyleSheet("font-family: monospace; font-size: 14px; padding: 4px;");
	videoLayout->addWidget(motionLevelLabel_);

	mainLayout->addWidget(videoGroup);

	/* --- Stream Quality Monitor (Optional) --- */
	QGroupBox *streamGroup = new QGroupBox("Stream Quality Monitor (Optional)", content);
	QVBoxLayout *streamLayout = new QVBoxLayout(streamGroup);

	streamQualityEnableCheck_ = new QCheckBox("Enable low-bitrate scene switching", streamGroup);
	streamLayout->addWidget(streamQualityEnableCheck_);

	QGridLayout *streamGrid = new QGridLayout();
	streamGrid->setColumnStretch(1, 1);
	streamGrid->addWidget(new QLabel("Switch to scene:", streamGroup), 0, 0);
	streamQualitySceneCombo_ = new QComboBox(streamGroup);
	streamGrid->addWidget(streamQualitySceneCombo_, 0, 1);

	bitrateThresholdLabel_ = new QLabel("2000 kbps", streamGroup);
	bitrateThresholdSlider_ = new QSlider(Qt::Horizontal, streamGroup);
	bitrateThresholdSlider_->setRange(500, 8000);
	bitrateThresholdSlider_->setValue(2000);
	bitrateThresholdSlider_->setSingleStep(100);
	streamGrid->addWidget(new QLabel("Min bitrate:", streamGroup), 1, 0);
	streamGrid->addWidget(bitrateThresholdSlider_, 1, 1);
	streamGrid->addWidget(bitrateThresholdLabel_, 1, 2);

	streamLayout->addLayout(streamGrid);

	streamStatsLabel_ = new QLabel("Bitrate: --- kbps | Dropped: 0% | Congestion: 0.0", streamGroup);
	streamStatsLabel_->setStyleSheet("font-family: monospace; font-size: 12px; padding: 4px;");
	streamLayout->addWidget(streamStatsLabel_);

	mainLayout->addWidget(streamGroup);

	/* --- Log --- */
	logDisplay_ = new QTextEdit(content);
	logDisplay_->setReadOnly(true);
	logDisplay_->setMaximumHeight(150);
	logDisplay_->setStyleSheet("font-family: monospace; font-size: 11px;");
	mainLayout->addWidget(new QLabel("Log:", content));
	mainLayout->addWidget(logDisplay_);

	mainLayout->addStretch();

	/* --- Timer --- */
	checkTimer_ = new QTimer(this);
	checkTimer_->setInterval(2000); /* 2 seconds */
	connect(checkTimer_, &QTimer::timeout, this, &AudioSwitcherDock::onCheckLevels);

	/* --- Connections --- */
	connect(startButton_, &QPushButton::clicked, this, &AudioSwitcherDock::onStartClicked);
	connect(stopButton_, &QPushButton::clicked, this, &AudioSwitcherDock::onStopClicked);
	connect(refreshButton_, &QPushButton::clicked, this, &AudioSwitcherDock::onRefreshScenes);
	connect(thresholdSlider_, &QSlider::valueChanged, this, &AudioSwitcherDock::onThresholdChanged);
	connect(sustainSlider_, &QSlider::valueChanged, this, &AudioSwitcherDock::onSustainChanged);
	connect(motionSensSlider_, &QSlider::valueChanged, this, &AudioSwitcherDock::onMotionSensitivityChanged);
	connect(motionSustainSlider_, &QSlider::valueChanged, this, &AudioSwitcherDock::onMotionSustainChanged);
	connect(streamQualityEnableCheck_, &QCheckBox::checkStateChanged, this, &AudioSwitcherDock::onStreamQualityEnableChanged);
	connect(bitrateThresholdSlider_, &QSlider::valueChanged, this, &AudioSwitcherDock::onBitrateThresholdChanged);

	/* Initial population */
	PopulateScenes();
	PopulateSources();
	PopulateVideoSources();
}

AudioSwitcherDock::~AudioSwitcherDock()
{
	if (checkTimer_)
		checkTimer_->stop();
	DestroyVideoResources();
}

void AudioSwitcherDock::Log(const QString &text)
{
	QString ts = QDateTime::currentDateTime().toString("hh:mm:ss");
	logDisplay_->append(QString("[%1] %2").arg(ts, text));
	QScrollBar *bar = logDisplay_->verticalScrollBar();
	bar->setValue(bar->maximum());
}

void AudioSwitcherDock::PopulateScenes()
{
	sceneACombo_->clear();
	sceneBCombo_->clear();

	char **names = obs_frontend_get_scene_names();
	if (!names)
		return;

	char **cur = names;
	while (*cur) {
		sceneACombo_->addItem(*cur);
		sceneBCombo_->addItem(*cur);
		cur++;
	}
	bfree(names);

	/* Default: A = first, B = second */
	if (sceneACombo_->count() > 0)
		sceneACombo_->setCurrentIndex(0);
	if (sceneBCombo_->count() > 1)
		sceneBCombo_->setCurrentIndex(1);
}

void AudioSwitcherDock::PopulateSources()
{
	sourceACombo_->clear();
	sourceBCombo_->clear();

	obs_enum_sources(
		[](void *param, obs_source_t *source) {
			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_AUDIO) {
				const char *name = obs_source_get_name(source);
				if (name) {
					auto *combo = static_cast<QComboBox *>(param);
					combo->addItem(name);
				}
			}
			return true;
		},
		sourceACombo_);

	obs_enum_sources(
		[](void *param, obs_source_t *source) {
			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_AUDIO) {
				const char *name = obs_source_get_name(source);
				if (name) {
					auto *combo = static_cast<QComboBox *>(param);
					combo->addItem(name);
				}
			}
			return true;
		},
		sourceBCombo_);

	/* Default: A = first, B = second */
	if (sourceACombo_->count() > 0)
		sourceACombo_->setCurrentIndex(0);
	if (sourceBCombo_->count() > 1)
		sourceBCombo_->setCurrentIndex(1);
}

void AudioSwitcherDock::PopulateVideoSources()
{
	videoSourceCombo_->clear();
	videoSceneCombo_->clear();

	/* Video sources: any source with video output */
	obs_enum_sources(
		[](void *param, obs_source_t *source) {
			uint32_t flags = obs_source_get_output_flags(source);
			if (flags & OBS_SOURCE_VIDEO) {
				const char *name = obs_source_get_name(source);
				if (name) {
					auto *combo = static_cast<QComboBox *>(param);
					combo->addItem(name);
				}
			}
			return true;
		},
		videoSourceCombo_);

	/* Scenes for video motion target and stream quality target */
	char **names = obs_frontend_get_scene_names();
	if (names) {
		char **cur = names;
		while (*cur) {
			videoSceneCombo_->addItem(*cur);
			streamQualitySceneCombo_->addItem(*cur);
			cur++;
		}
		bfree(names);
	}
}

void AudioSwitcherDock::onRefreshScenes()
{
	PopulateScenes();
	PopulateSources();
	PopulateVideoSources();
	Log("Refreshed scenes and sources");
}

void AudioSwitcherDock::onThresholdChanged(int value)
{
	thresholdDb_ = (float)value;
	thresholdLabel_->setText(QString("%1 dB").arg(thresholdDb_, 0, 'f', 1));
}

void AudioSwitcherDock::onSustainChanged(int value)
{
	sustainSecs_ = (float)value;
	sustainLabel_->setText(QString("%1 s").arg(sustainSecs_, 0, 'f', 1));
}

void AudioSwitcherDock::onMotionSensitivityChanged(int value)
{
	motionSensitivity_ = (float)value;
	motionSensLabel_->setText(QString("%1%").arg(value));
}

void AudioSwitcherDock::onMotionSustainChanged(int value)
{
	motionSustainSecs_ = (float)value;
	motionSustainLabel_->setText(QString("%1 s").arg((double)value, 0, 'f', 1));
}

void AudioSwitcherDock::onStreamQualityEnableChanged(Qt::CheckState state)
{
	streamQualitySwitched_ = false;
	lowBitrateSince_ = 0;
	lastTotalBytes_ = 0;
}

void AudioSwitcherDock::onBitrateThresholdChanged(int value)
{
	bitrateThresholdKbps_ = value;
	bitrateThresholdLabel_->setText(QString("%1 kbps").arg(value));
}

void AudioSwitcherDock::onStartClicked()
{
	if (running_)
		return;

	QString sceneA = sceneACombo_->currentText();
	QString sceneB = sceneBCombo_->currentText();
	QString sourceA = sourceACombo_->currentText();
	QString sourceB = sourceBCombo_->currentText();

	if (sceneA.isEmpty() || sceneB.isEmpty()) {
		Log("Error: Select both scenes first");
		return;
	}
	if (sourceA.isEmpty() || sourceB.isEmpty()) {
		Log("Error: Select both audio sources first");
		return;
	}
	if (sceneA == sceneB) {
		Log("Error: Scene A and B must be different");
		return;
	}

	running_ = true;
	switchCount_ = 0;
	aDominantSince_ = 0;
	bDominantSince_ = 0;
	motionDominantSince_ = 0;
	hasPrevFrame_ = false;
	lowBitrateSince_ = 0;
	lastTotalBytes_ = 0;
	streamQualitySwitched_ = false;
	switchCountLabel_->setText("Switches: 0");

	startButton_->setEnabled(false);
	stopButton_->setEnabled(true);
	statusLabel_->setText("Status: Monitoring");
	statusLabel_->setStyleSheet("padding: 4px; color: #4a9;");

	Log(QString("Started: '%1' vs '%2' -> '%3' / '%4' (threshold: %5dB, sustain: %6s)")
		    .arg(sourceA, sourceB, sceneA, sceneB)
		    .arg(thresholdDb_, 0, 'f', 1)
		    .arg(sustainSecs_, 0, 'f', 1));

	/* Ensure both sources are in both scenes (hidden) for continuous monitoring */
	PythonBridge::EnsureAllVolmeters();
	PythonBridge::EnsureSourceInScene(sourceA.toStdString(), sceneA.toStdString());
	PythonBridge::EnsureSourceInScene(sourceA.toStdString(), sceneB.toStdString());
	PythonBridge::EnsureSourceInScene(sourceB.toStdString(), sceneA.toStdString());
	PythonBridge::EnsureSourceInScene(sourceB.toStdString(), sceneB.toStdString());
	Log("Ensured both sources are in both scenes (hidden for audio monitoring)");

	/* Init video resources if motion detection enabled */
	if (videoEnableCheck_->isChecked()) {
		InitVideoResources();
		Log(QString("Video motion detection enabled: monitoring '%1' -> switch to '%2' (sensitivity: %3%, sustain: %4s)")
			    .arg(videoSourceCombo_->currentText(),
			         videoSceneCombo_->currentText())
			    .arg(motionSensitivity_, 0, 'f', 0)
			    .arg(motionSustainSecs_, 0, 'f', 1));
	}

	checkTimer_->start();
}

void AudioSwitcherDock::onStopClicked()
{
	if (!running_)
		return;

	running_ = false;
	checkTimer_->stop();
	DestroyVideoResources();

	startButton_->setEnabled(true);
	stopButton_->setEnabled(false);
	statusLabel_->setText("Status: Idle");
	statusLabel_->setStyleSheet("padding: 4px; color: #888;");

	Log(QString("Stopped. Total switches: %1").arg(switchCount_));
}

void AudioSwitcherDock::onCheckLevels()
{
	if (!running_)
		return;

	QString sourceA = sourceACombo_->currentText();
	QString sourceB = sourceBCombo_->currentText();
	QString sceneA = sceneACombo_->currentText();
	QString sceneB = sceneBCombo_->currentText();

	float levelA = PythonBridge::GetSourceMagnitudeDb(sourceA.toStdString());
	float levelB = PythonBridge::GetSourceMagnitudeDb(sourceB.toStdString());

	/* Update level display */
	QString colorA = levelA > -60 ? "color: #4a9;" : "color: #888;";
	QString colorB = levelB > -60 ? "color: #4a9;" : "color: #888;";
	levelALabel_->setText(QString("Source A: %1 dB").arg(levelA, 6, 'f', 1));
	levelALabel_->setStyleSheet(QString("font-family: monospace; font-size: 14px; padding: 4px; %1").arg(colorA));
	levelBLabel_->setText(QString("Source B: %1 dB").arg(levelB, 6, 'f', 1));
	levelBLabel_->setStyleSheet(QString("font-family: monospace; font-size: 14px; padding: 4px; %1").arg(colorB));

	/* Get current scene */
	obs_source_t *current = obs_frontend_get_current_scene();
	QString currentScene;
	if (current) {
		currentScene = obs_source_get_name(current);
		obs_source_release(current);
	}

	/* A/B switching logic */
	float now = (float)QDateTime::currentDateTime().toSecsSinceEpoch();
	const float MIN_LEVEL = -60.0f;

	if (levelA > levelB + thresholdDb_ && levelA > MIN_LEVEL) {
		if (aDominantSince_ == 0)
			aDominantSince_ = now;
		bDominantSince_ = 0;

		if (now - aDominantSince_ >= sustainSecs_ && currentScene != sceneA) {
			Log(QString(">>> Switching to '%1' (%2: %3dB > %4: %5dB)")
				    .arg(sceneA, sourceA)
				    .arg(levelA, 0, 'f', 1)
				    .arg(sourceB)
				    .arg(levelB, 0, 'f', 1));

			obs_frontend_source_list scenes = {};
			obs_frontend_get_scenes(&scenes);
			for (size_t i = 0; i < scenes.sources.num; i++) {
				const char *sname = obs_source_get_name(scenes.sources.array[i]);
				if (sname && sceneA == sname) {
					obs_frontend_set_current_scene(scenes.sources.array[i]);
					break;
				}
			}
			obs_frontend_source_list_free(&scenes);

			switchCount_++;
			switchCountLabel_->setText(QString("Switches: %1").arg(switchCount_));
		}
	} else if (levelB > levelA + thresholdDb_ && levelB > MIN_LEVEL) {
		if (bDominantSince_ == 0)
			bDominantSince_ = now;
		aDominantSince_ = 0;

		if (now - bDominantSince_ >= sustainSecs_ && currentScene != sceneB) {
			Log(QString(">>> Switching to '%1' (%2: %3dB > %4: %5dB)")
				    .arg(sceneB, sourceB)
				    .arg(levelB, 0, 'f', 1)
				    .arg(sourceA)
				    .arg(levelA, 0, 'f', 1));

			obs_frontend_source_list scenes = {};
			obs_frontend_get_scenes(&scenes);
			for (size_t i = 0; i < scenes.sources.num; i++) {
				const char *sname = obs_source_get_name(scenes.sources.array[i]);
				if (sname && sceneB == sname) {
					obs_frontend_set_current_scene(scenes.sources.array[i]);
					break;
				}
			}
			obs_frontend_source_list_free(&scenes);

			switchCount_++;
			switchCountLabel_->setText(QString("Switches: %1").arg(switchCount_));
		}
	} else {
		aDominantSince_ = 0;
		bDominantSince_ = 0;
	}

	/* --- Video Motion Detection (independent path) --- */
	if (videoEnableCheck_->isChecked() && videoResourcesReady_) {
		QString videoSource = videoSourceCombo_->currentText();
		QString videoScene = videoSceneCombo_->currentText();

		if (!videoSource.isEmpty() && !videoScene.isEmpty()) {
			float motion = CheckVideoMotion(videoSource);
			lastMotionLevel_ = motion;

			QString motionColor = motion > motionSensitivity_ ? "color: #e94;" : "color: #888;";
			motionLevelLabel_->setText(QString("Motion: %1%").arg(motion, 0, 'f', 1));
			motionLevelLabel_->setStyleSheet(
				QString("font-family: monospace; font-size: 14px; padding: 4px; %1").arg(motionColor));

			if (motion > motionSensitivity_) {
				if (motionDominantSince_ == 0)
					motionDominantSince_ = now;

				if (now - motionDominantSince_ >= motionSustainSecs_ && currentScene != videoScene) {
					Log(QString(">>> Motion switch to '%1' (motion: %2% > %3%)")
						    .arg(videoScene)
						    .arg(motion, 0, 'f', 1)
						    .arg(motionSensitivity_, 0, 'f', 0));

					obs_frontend_source_list scenes = {};
					obs_frontend_get_scenes(&scenes);
					for (size_t i = 0; i < scenes.sources.num; i++) {
						const char *sname = obs_source_get_name(scenes.sources.array[i]);
						if (sname && videoScene == sname) {
							obs_frontend_set_current_scene(scenes.sources.array[i]);
							break;
						}
					}
					obs_frontend_source_list_free(&scenes);

					switchCount_++;
					switchCountLabel_->setText(QString("Switches: %1").arg(switchCount_));
				}
			} else {
				motionDominantSince_ = 0;
			}
		}
	}

	/* --- Stream Quality Monitor (independent path) --- */
	if (streamQualityEnableCheck_->isChecked()) {
		obs_output_t *output = obs_frontend_get_streaming_output();
		if (output) {
			uint64_t totalBytes = obs_output_get_total_bytes(output);
			int dropped = obs_output_get_frames_dropped(output);
			int totalFrames = obs_output_get_total_frames(output);
			float congestion = obs_output_get_congestion(output);

			/* Calculate bitrate from bytes delta over the check interval */
			float bitrateKbps = 0;
			uint64_t bytesDelta = 0;
			if (lastTotalBytes_ > 0 && totalBytes > lastTotalBytes_) {
				bytesDelta = totalBytes - lastTotalBytes_;
				/* bytes / seconds * 8 / 1000 = kbps */
				bitrateKbps = (float)bytesDelta / 2.0f * 8.0f / 1000.0f;
			}
			lastTotalBytes_ = totalBytes;
			lastBitrateKbps_ = bitrateKbps;

			float dropPct = (totalFrames > 0) ? (float)dropped / totalFrames * 100.0f : 0.0f;

			QString statsColor = (bitrateKbps > 0 && bitrateKbps < bitrateThresholdKbps_) ? "color: #e44;" : "color: #4a9;";
			streamStatsLabel_->setText(
				QString("Bitrate: %1 kbps | Dropped: %2% | Congestion: %3")
					.arg(bitrateKbps, 0, 'f', 0)
					.arg(dropPct, 0, 'f', 1)
					.arg(congestion, 0, 'f', 2));
			streamStatsLabel_->setStyleSheet(
				QString("font-family: monospace; font-size: 12px; padding: 4px; %1").arg(statsColor));

			QString qualityScene = streamQualitySceneCombo_->currentText();

			if (bitrateKbps > 0 && bitrateKbps < bitrateThresholdKbps_) {
				if (lowBitrateSince_ == 0)
					lowBitrateSince_ = now;

				/* Switch after 4 seconds of low bitrate (2 checks) */
				if (now - lowBitrateSince_ >= 4.0f && !streamQualitySwitched_ &&
				    !qualityScene.isEmpty() && currentScene != qualityScene) {
					Log(QString(">>> Low bitrate switch to '%1' (%2 kbps < %3 kbps)")
						    .arg(qualityScene)
						    .arg(bitrateKbps, 0, 'f', 0)
						    .arg(bitrateThresholdKbps_));

					obs_frontend_source_list scenes = {};
					obs_frontend_get_scenes(&scenes);
					for (size_t i = 0; i < scenes.sources.num; i++) {
						const char *sname = obs_source_get_name(scenes.sources.array[i]);
						if (sname && qualityScene == sname) {
							obs_frontend_set_current_scene(scenes.sources.array[i]);
							break;
						}
					}
					obs_frontend_source_list_free(&scenes);

					streamQualitySwitched_ = true;
					switchCount_++;
					switchCountLabel_->setText(QString("Switches: %1").arg(switchCount_));
				}
			} else if (bitrateKbps >= bitrateThresholdKbps_) {
				/* Bitrate recovered - allow switching again */
				lowBitrateSince_ = 0;
				streamQualitySwitched_ = false;
			}

			obs_output_release(output);
		}
	}
}

/* --- Video Motion Detection Implementation --- */

void AudioSwitcherDock::InitVideoResources()
{
	if (videoResourcesReady_)
		return;

	videoTexrender_ = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
	videoStaging_ = gs_stagesurface_create(VIDEO_SAMPLE_W, VIDEO_SAMPLE_H, GS_RGBA);
	prevFrame_ = (uint8_t *)bmalloc(VIDEO_FRAME_SIZE);
	hasPrevFrame_ = false;
	videoResourcesReady_ = (videoTexrender_ && videoStaging_ && prevFrame_);
}

void AudioSwitcherDock::DestroyVideoResources()
{
	if (videoTexrender_) {
		gs_texrender_destroy((gs_texrender_t *)videoTexrender_);
		videoTexrender_ = nullptr;
	}
	if (videoStaging_) {
		gs_stagesurface_destroy((gs_stagesurf_t *)videoStaging_);
		videoStaging_ = nullptr;
	}
	if (prevFrame_) {
		bfree(prevFrame_);
		prevFrame_ = nullptr;
	}
	hasPrevFrame_ = false;
	videoResourcesReady_ = false;
}

float AudioSwitcherDock::CheckVideoMotion(const QString &sourceName)
{
	obs_source_t *source = obs_get_source_by_name(sourceName.toUtf8().constData());
	if (!source)
		return 0.0f;

	uint32_t srcW = obs_source_get_width(source);
	uint32_t srcH = obs_source_get_height(source);
	if (srcW == 0 || srcH == 0) {
		obs_source_release(source);
		return 0.0f;
	}

	gs_texrender_t *tr = (gs_texrender_t *)videoTexrender_;
	gs_stagesurf_t *staging = (gs_stagesurf_t *)videoStaging_;

	gs_texrender_reset(tr);
	if (!gs_texrender_begin(tr, srcW, srcH)) {
		obs_source_release(source);
		return 0.0f;
	}

	struct vec4 clearVal;
	vec4_set(&clearVal, 0, 0, 0, 0);
	gs_clear(GS_CLEAR_COLOR, &clearVal, 1.0f, 0);
	gs_ortho(0.0f, (float)srcW, 0.0f, (float)srcH, -100.0f, 100.0f);

	obs_source_video_render(source);
	gs_texrender_end(tr);

	gs_texture_t *tex = gs_texrender_get_texture(tr);
	if (!tex) {
		obs_source_release(source);
		return 0.0f;
	}

	/* Stage: copy texture to CPU staging surface at reduced resolution */
	gs_stage_texture(staging, tex);

	uint8_t *data = nullptr;
	uint32_t linesize = 0;
	if (!gs_stagesurface_map(staging, &data, &linesize)) {
		obs_source_release(source);
		return 0.0f;
	}

	/* Copy staged data to a temp buffer */
	uint8_t curFrame[VIDEO_FRAME_SIZE];
	for (uint32_t y = 0; y < VIDEO_SAMPLE_H; y++) {
		memcpy(curFrame + y * VIDEO_SAMPLE_W * 4,
		       data + y * linesize,
		       VIDEO_SAMPLE_W * 4);
	}
	gs_stagesurface_unmap(staging);
	obs_source_release(source);

	/* Compare with previous frame */
	if (!hasPrevFrame_) {
		memcpy(prevFrame_, curFrame, VIDEO_FRAME_SIZE);
		hasPrevFrame_ = true;
		return 0.0f;
	}

	int diffPixels = 0;
	int totalPixels = VIDEO_SAMPLE_W * VIDEO_SAMPLE_H;

	for (int i = 0; i < totalPixels; i++) {
		int dr = abs((int)curFrame[i * 4] - (int)prevFrame_[i * 4]);
		int dg = abs((int)curFrame[i * 4 + 1] - (int)prevFrame_[i * 4 + 1]);
		int db = abs((int)curFrame[i * 4 + 2] - (int)prevFrame_[i * 4 + 2]);
		if (dr + dg + db > 30) /* per-pixel threshold */
			diffPixels++;
	}

	memcpy(prevFrame_, curFrame, VIDEO_FRAME_SIZE);

	return (float)diffPixels / totalPixels * 100.0f;
}
