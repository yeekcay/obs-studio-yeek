cmake_minimum_required(VERSION 3.28...3.30)

find_package(Qt6 REQUIRED Widgets)

add_library(streamdirector-agent MODULE)
add_library(OBS::streamdirector-agent ALIAS streamdirector-agent)

target_sources(
  streamdirector-agent
  PRIVATE
    plugin-main.cpp
    PythonBridge.cpp
    PythonBridge.hpp
    AgentDock.cpp
    AgentDock.hpp
    AudioSwitcherDock.cpp
    AudioSwitcherDock.hpp
)

target_link_libraries(
  streamdirector-agent
  PRIVATE
    OBS::frontend-api
    OBS::libobs
    Qt::Widgets
)

# Find Python for embedding
# Try find_package first, fall back to explicit paths
find_package(Python3 COMPONENTS Development)
if(NOT Python3_FOUND)
  # Fallback: use explicit paths for common Windows Python installations
  if(EXISTS "C:/Python313/include/Python.h")
    set(Python3_INCLUDE_DIRS "C:/Python313/include")
    set(Python3_LIBRARIES "C:/Python313/libs/python313.lib")
  elseif(EXISTS "C:/Python312/include/Python.h")
    set(Python3_INCLUDE_DIRS "C:/Python312/include")
    set(Python3_LIBRARIES "C:/Python312/libs/python312.lib")
  elseif(EXISTS "C:/Python311/include/Python.h")
    set(Python3_INCLUDE_DIRS "C:/Python311/include")
    set(Python3_LIBRARIES "C:/Python311/libs/python311.lib")
  else()
    message(FATAL_ERROR "Python3 development headers not found. Install Python3 with development headers.")
  endif()
endif()
target_include_directories(streamdirector-agent PRIVATE ${Python3_INCLUDE_DIRS})
target_link_libraries(streamdirector-agent PRIVATE ${Python3_LIBRARIES})

set_target_properties_obs(
  streamdirector-agent
  PROPERTIES FOLDER plugins
             PREFIX ""
             AUTOMOC ON
)

# Copy Python DLLs to runtime directory for embedded Python
if(OS_WINDOWS)
  # Determine Python DLL path from the lib we linked against
  get_filename_component(_py_lib_dir "${Python3_LIBRARIES}" DIRECTORY)
  get_filename_component(_py_lib_name "${Python3_LIBRARIES}" NAME_WE)
  string(REGEX REPLACE "python" "python" _py_dll_name "${_py_lib_name}")
  set(_py_dll_path "${_py_lib_dir}/../${_py_dll_name}.dll")
  # python3.dll is the stable ABI stub that native .pyd extensions link against
  set(_py3_dll_path "${_py_lib_dir}/../python3.dll")

  if(EXISTS "${_py_dll_path}")
    add_custom_command(
      TARGET streamdirector-agent
      POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E echo "Copy Python DLL to runtime directory"
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${_py_dll_path}"
              "${OBS_OUTPUT_DIR}/$<CONFIG>/${OBS_EXECUTABLE_DESTINATION}"
      COMMENT ""
      VERBATIM
    )
    install(FILES "${_py_dll_path}" DESTINATION "${OBS_EXECUTABLE_DESTINATION}" COMPONENT Runtime)
  endif()

  if(EXISTS "${_py3_dll_path}")
    add_custom_command(
      TARGET streamdirector-agent
      POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E echo "Copy python3.dll to runtime directory"
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${_py3_dll_path}"
              "${OBS_OUTPUT_DIR}/$<CONFIG>/${OBS_EXECUTABLE_DESTINATION}"
      COMMENT ""
      VERBATIM
    )
    install(FILES "${_py3_dll_path}" DESTINATION "${OBS_EXECUTABLE_DESTINATION}" COMPONENT Runtime)
  endif()
endif()
