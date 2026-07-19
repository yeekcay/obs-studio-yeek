cmake_minimum_required(VERSION 3.28...3.30)

find_package(Qt6 REQUIRED Widgets Network)

add_library(streamdirector-alerts MODULE)
add_library(OBS::streamdirector-alerts ALIAS streamdirector-alerts)

target_sources(
  streamdirector-alerts
  PRIVATE
    plugin-main.cpp
)

target_include_directories(
  streamdirector-alerts
  PRIVATE
    ${CMAKE_SOURCE_DIR}/plugins/obs-browser/panel
)

target_link_libraries(
  streamdirector-alerts
  PRIVATE
    OBS::frontend-api
    OBS::libobs
    Qt::Widgets
    Qt::Network
)

set_target_properties_obs(
  streamdirector-alerts
  PROPERTIES FOLDER plugins
             PREFIX ""
             AUTOMOC ON
)

# Copy data files to data directory
add_custom_command(
  TARGET streamdirector-alerts
  POST_BUILD
  COMMAND "${CMAKE_COMMAND}" -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/data"
          "${OBS_OUTPUT_DIR}/$<CONFIG>/${OBS_DATA_DESTINATION}/obs-plugins/streamdirector-alerts"
  COMMENT "Copy streamdirector-alerts data files to data directory"
  VERBATIM
)
