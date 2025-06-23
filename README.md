[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.15721134.svg)](https://doi.org/10.5281/zenodo.15721134)

This repository outlines the development of a simple digital signage solution, leveraging a Raspberry Pi and the Vimeo API. Designed for dynamic art installations or informational displays, the system automates the scheduled refresh of video content: initiating a morning cycle to cease any ongoing playback, purge previous media, procure the latest video asset from a specified Vimeo source, and then commence continuous, hardware-accelerated playback until the subsequent daily refresh. Conceptually:

![image](https://github.com/user-attachments/assets/25e4f190-e87c-4053-a37d-f95d5c11ddee)


At its core, the system operates on a fixed-duration cyclical refresh mechanism, orchestrated by a Python-based Digital Signage Script. This script is intended to run perpetually, managing content acquisition and playback with precise temporal control.

Upon the commencement of each cycle, the Python script first asserts control over any active video playback. Should an instance of FFplay (the chosen multimedia player) from a preceding cycle be operational, it is forcefully terminated to ensure an immediate and seamless transition to new content. Concurrently, the local storage designated for media assets is purged, ensuring efficient disk space management.

Subsequently, the script initiates communication with the Vimeo API using a pre-configured Personal Access Token. It systematically queries a specified Vimeo Project (or Folder) to retrieve a comprehensive list of all associated video metadata, with a keen focus on discerning direct .mp4 download links. These links, typically provided within the files array of the API response (or falling back to the download array or parsed embed.html for progressive links), are then meticulously sorted according to a predefined nomenclature (e.g., BLACKLIVES_BL_XXX), thus preserving a desired playback order.

Following the acquisition of the ordered playlist, the script consults a local state file to ascertain the index of the video processed in the prior cycle. It then determines and downloads the next sequential video asset to the local storage. Upon successful download, FFplay is invoked to commence playback of this newly acquired video. Crucially, FFplay is executed as a non-blocking subprocess, allowing the main Python script to continue its execution independently of the player's duration.

The overarching main loop within the Python script diligently monitors the elapsed time of the current cycle. Upon completion of the content acquisition and playback initiation phases, the script calculates a time.sleep() duration. This calculated pause ensures that the total elapsed time between the initiation of one cycle and the initiation of the subsequent cycle precisely adheres to the TARGET_CYCLE_DURATION_SECONDS. This mechanism guarantees consistent content refreshing at predetermined intervals, regardless of individual video lengths, thereby enabling the "take over" functionality vital for dynamic display environments. Robust logging is integrated throughout to provide comprehensive operational insights and facilitate debugging.

The Python code within this repository directly implements the described operational logic, thus:

![image](https://github.com/user-attachments/assets/edffdc62-273c-4ce8-863b-c57bce69026f)




