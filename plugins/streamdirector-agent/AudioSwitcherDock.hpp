#pragma once

#include <QDockWidget>
#include <QComboBox>
#include <QPushButton>
#include <QLabel>
#include <QSlider>
#include <QTimer>
#include <QTextEdit>
#include <QCheckBox>
#include <string>

#include <obs-frontend-api.h>

class AudioSwitcherDock : public QDockWidget {
	Q_OBJECT

public:
	explicit AudioSwitcherDock(QWidget *parent = nullptr);
	~AudioSwitcherDock();

private slots:
	void onStartClicked();
	void onStopClicked();
	void onRefreshScenes();
	void onCheckLevels();
	void onThresholdChanged(int value);
	void onSustainChanged(int value);
	void onMotionSensitivityChanged(int value);
	void onMotionSustainChanged(int value);
	void onStreamQualityEnableChanged(Qt::CheckState state);
	void onBitrateThresholdChanged(int value);

private:
	void PopulateScenes();
	void PopulateAll();
	void onFrontendEvent(enum obs_frontend_event event);
	void PopulateSources();
	void PopulateVideoSources();
	void Log(const QString &text);

	/* Video motion detection */
	float CheckVideoMotion(const QString &sourceName);
	void InitVideoResources();
	void DestroyVideoResources();

	QComboBox *sceneACombo_;
	QComboBox *sceneBCombo_;
	QComboBox *sourceACombo_;
	QComboBox *sourceBCombo_;

	QSlider *thresholdSlider_;
	QSlider *sustainSlider_;
	QLabel *thresholdLabel_;
	QLabel *sustainLabel_;

	QLabel *levelALabel_;
	QLabel *levelBLabel_;
	QLabel *statusLabel_;
	QLabel *switchCountLabel_;

	QPushButton *startButton_;
	QPushButton *stopButton_;
	QPushButton *refreshButton_;

	QTextEdit *logDisplay_;

	QTimer *checkTimer_;

	/* Video motion UI */
	QCheckBox *videoEnableCheck_;
	QComboBox *videoSourceCombo_;
	QComboBox *videoSceneCombo_;
	QSlider *motionSensSlider_;
	QSlider *motionSustainSlider_;
	QLabel *motionSensLabel_;
	QLabel *motionSustainLabel_;
	QLabel *motionLevelLabel_;

	/* Stream quality UI */
	QCheckBox *streamQualityEnableCheck_;
	QComboBox *streamQualitySceneCombo_;
	QSlider *bitrateThresholdSlider_;
	QLabel *bitrateThresholdLabel_;
	QLabel *streamStatsLabel_;

	/* State - audio */
	float thresholdDb_;
	float sustainSecs_;
	float aDominantSince_;
	float bDominantSince_;
	int switchCount_;
	bool running_;

	/* State - video motion */
	float motionSensitivity_;
	float motionSustainSecs_;
	float motionDominantSince_;
	float lastMotionLevel_;
	bool videoResourcesReady_;

	/* Graphics resources (opaque pointers, cast in .cpp) */
	void *videoTexrender_;
	void *videoStaging_;
	uint8_t *prevFrame_;
	bool hasPrevFrame_;

	/* State - stream quality */
	int bitrateThresholdKbps_;
	uint64_t lastTotalBytes_;
	float lastBitrateKbps_;
	float lowBitrateSince_;
	bool streamQualitySwitched_;
};
