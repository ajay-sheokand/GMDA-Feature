# Gardony Map Drawing Analyzer (GMDA) - SketchMapia Feature

## Overview
This repo provides a Python implementation of the Gardony Map Drawing Analyzer (GMDA) metrics, designed to evaluate spatial memory and cognitive map accuracy. Originally introduced by Gardony et al., these metrics assess the spatial distortion between a sketchmap (drawn) and a basemap (target). 

This specific implementation has been adpated for modern geospatial workflows, processing spatial data via GeoJSON and utilizing Minimum Bounding Rectangles (MBRs).

## Key Features

- Geospatial Compatibility: Directly ingests GeoJSON feature collections representing landmarks.
- Advanced Mode Support: Implements the paper's "Advanced Mode" by using 8 peripheral points along the MBR of each landmark instead of the "Basic Mode", which uses a Single Centroid. This accurately captures both the position and the spatial extend/orientation of the drawn landmarks or objects.
- Robust Angular Math: It uses the trigonometric summation (np.arctan2) to accurately calculate circular means, gracefully handling the  $0^\circ \equiv 360^\circ$ wrap-around.
- Strict 1-to-1 Alignment: Utilizes a Union-Find structure via a "SketchAlign" attribute to group features, filtering for strict 1-to-1 matches to prevent severe distortion of metrics.

## Combinatorics (Advanced Mode)

Since, this method represents each landmark using 8 peripheral points. It generates a massive number of pairwise comparisons. To prevent the peripheral points belonging to the same landmark from being compared to one another, the total number of valid comparisons is strictly calculated.

Let $n_{TL}$ be the number of total target landmarks, and $n_{DL}$ be the number of drawn sketch landmarks. The total number of pairwise comparisons ($N$) is defined as:For Total Target Landmarks ($N_{TL}$):$$N_{TL} = \binom{8n_{TL}}{2} - n_{TL}\binom{8}{2}$$For Drawn Landmarks ($N_{DL}$):$$N_{DL} = \binom{8n_{DL}}{2} - n_{DL}\binom{8}{2}$$


## Metrics Calculated

This service outputs a dictionary containing the following core spatial metrics:

1. **Canonical Organization (CanOrg)**:  
Measures the overall spatial organization and topological accuracy (N/S/E/W relationships). It uses the total possible landmark pairs ($N_{TL}$) as the denominator, intentionally penalizing the score for any omitted/forgotten landmarks.

$$CanOrg = \frac{\sum_{i=1}^{N_{TL}} \text{canonical\_score}_i}{2N_{TL}}$$

2. **Canonical Accuracy (CanAcc)**:  
Tsolates the accuracy of the spatial layout from recall completeness. It switches the denominator to the drawn landmark pairs ($N_{DL}$), meaning it does not penalize the user for missing landmarks, only for the placement of the landmarks they did draw.

$$CanAcc = \frac{\sum_{i=1}^{N_{DL}} \text{canonical\_score}_i}{2N_{DL}}$$

3. **Distance Accuracy (DistAcc)**:  
Calculates the magnitude of distance error between landmark pairs, scale-equalized and normalized to a score between 0 and 1. Let $dr_{SM}$ and $dr_{TE}$ be the distance ratios (distance divided by max distance) for the sketch map and target environment, respectively.

$$DistAcc = 1 - \frac{\sum_{i=1}^{N_{DL}} |dr_{SM, i} - dr_{TE, i}|}{N_{DL}}$$


4. **Scaling Bias (ScaBias)**:  
Tracks the directional expansion or compression of the map by evaluating scale-equalized distance ratios. Positive values indicate expansion, while negative values indicate compression.

$$ScaBias = \frac{\sum_{i=1}^{N_{DL}} (dr_{SM, i} - dr_{TE, i})}{N_{DL}}$$

5. **Angular Accuracy (AngAcc)**:  
Averages the absolute angular deviations ($ang_{Diff}$) between target and drawn landmark pairs. It scales the errors against the maximum possible error ($180^\circ$) to produce a normalized score between 0 and 1.

$$AngAcc = 1 - \frac{\sum_{i=1}^{N_{DL}} \left| \frac{180}{\pi} ang_{Diff, i} \right|}{180 \cdot N_{DL}}$$


6. **Rotational Bias (RotBias)**:  
Computes the circular mean of angular differences to identify systematic rotational skewing of the entire drawn map compared to the reference. Positive values indicate clockwise rotation, and negative indicate counterclockwise.

$$RotBias = \frac{180}{\pi} \text{atan2}\left( \frac{\sum_{i=1}^{N_{DL}} \sin(ang_{Diff, i})}{N_{DL}}, \frac{\sum_{i=1}^{N_{DL}} \cos(ang_{Diff, i})}{N_{DL}} \right)$$


