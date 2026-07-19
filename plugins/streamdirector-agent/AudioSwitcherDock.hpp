#pragma once

#include <QDockWidget>
#include <QComboBox>
#include <QPushButton>
#include <QLabel>
#include <QSlider>
#include <QTimer>
#include <QTextEdit>
#include <string>

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

private:
	void PopulateScenes();
	void PopulateSources();
	void Log(const QString &text);

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

	/* State */
	float thresholdDb_;
	float sustainSecs_;
	float aDominantSince_;
	float bDominantSince_;
	int switchCount_;
	bool running_;
};
