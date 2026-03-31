# unity_tools
Collection of tools/scripts to improve the Unity experience

## project_generator

A Python script that helps generated a Unity project with fewer dependencies than the built-in templates.

### Example:

```
pytnon clone_blank_project.py --template BlankProject6000.3 --profile barebones-builtin --addons openxr --ide msvs BlankXRProject
```

Creates a built-in pipeline project with OpenXR and support for Visual Studio IDE.

To see more options:

```
pytnon clone_blank_project.py --help
```
