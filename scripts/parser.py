# coding: utf-8
from __future__ import print_function, division
from logger import logger

import copy
import json
import string
import urllib
import os

from catalog import Catalog
from export import coords2geojson
from scripts.merge_tiles import PkkAreaMerger
from utils import xy2lonlat, make_request, TimeoutException

try:
    import urlparse
    from urllib import urlencode
except ImportError:  # For Python 3
    import urllib.parse as urlparse
    from urllib.parse import urlencode


##############
# SEARCH URL #
##############
# http://pkk5.rosreestr.ru/api/features/1
#   ?text=38:36:000021:1106
#   &tolerance=4
#   &limit=11
SEARCH_URL = "http://pkk5.rosreestr.ru/api/features/$area_type"

############################
# URL to get area metainfo #
############################
# http://pkk5.rosreestr.ru/api/features/1/38:36:21:1106
FEATURE_INFO_URL = "http://pkk5.rosreestr.ru/api/features/$area_type/"

#########################
# URL to get area image #
#########################
# http://pkk5.rosreestr.ru/arcgis/rest/services/Cadastre/CadastreSelected/MapServer/export
#   ?dpi=96
#   &transparent=true
#   &format=png32
#   &layers=show%3A6%2C7
#   &bbox=11612029.005008286%2C6849457.6834302815%2C11612888.921576614%2C6849789.706771941
#   &bboxSR=102100
#   &imageSR=102100
#   &size=1440%2C556
#   &layerDefs=%7B%226%22%3A%22ID%20%3D%20%2738%3A36%3A21%3A1106%27%22%2C%227%22%3A%22ID%20%3D%20%2738%3A36%3A21%3A1106%27%22%7D
#   &f=image
# WHERE:
#    "layerDefs" decode to {"6":"ID = '38:36:21:1106'","7":"ID = '38:36:21:1106'"}
#    "f" may be `json` or `html`
#    set `&format=svg&f=json` to export image in svg !closed by rosreestr, now only PNG
IMAGE_URL = "http://pkk5.rosreestr.ru/arcgis/rest/services/Cadastre/CadastreSelected/MapServer/export"

TYPES = {
    u"Участки": 1,
    u"ОКС": 5,
    u"Кварталы": 2,
    u"Районы": 3,
    u"Округа": 4,
    u"Границы": 7,
    u"ЗОУИТ": 10,
    u"Тер. зоны": 6,
    u"Красные линии": 13,
    u"Лес": 12,
    u"СРЗУ": 15,
    u"ОЭЗ": 16,
    u"ГОК": 9,
}


class NoCoordinatesException(Exception):
    pass



# def restore_area(restore, area_type=1, media_path="", with_log=False, catalog_path="", coord_out="EPSG:3857",
#                  file_name="example", output=os.path.join("output"), repeat=0, areas=None, with_attrs=False, delay=1,
#                  center_only=False, with_proxy=False):
#     area = Area(media_path=media_path, area_type=area_type, with_log=with_log, coord_out=coord_out,
#                             center_only=center_only, with_proxy=with_proxy)

def restore_area(restore, area_type=1, media_path="", with_log=False, catalog_path="", coord_out="EPSG:3857",
                 file_name="example", output=os.path.join("output"), repeat=0, areas=None, with_attrs=False, delay=1,
                 center_only=False, with_proxy=False):
    area = Area(media_path=media_path, area_type=area_type, with_log=with_log, coord_out=coord_out,
                center_only=center_only, with_proxy=with_proxy)
    area.restore(restore)
    return area


class Area:
    image_url = IMAGE_URL
    buffer = 10
    save_attrs = ["code", "area_type", "attrs", "image_path", "center", "extent", "image_extent", "width", "height"]

    def __init__(self, code="", area_type=1, epsilon=5, media_path="", with_log=True, catalog="",
                 coord_out="EPSG:3857", center_only=False, with_proxy=False):
        self.with_log = with_log
        self.area_type = area_type
        self.media_path = media_path
        self.image_url = ""
        self.xy = []  # [[[area1], [hole1], [holeN]], [[area2]]]
        self.image_xy_corner = []  # cartesian coord from image, for draw plot
        self.width = 0
        self.height = 0
        self.image_path = ""
        self.extent = {}
        self.image_extent = {}
        self.center = {'x': None, 'y': None}
        self.center_only = center_only
        self.attrs = {}
        self.epsilon = epsilon
        self.code = code
        self.code_id = ""
        self.file_name = self.code[:].replace(":", "_")
        self.with_proxy = with_proxy

        self.coord_out = coord_out

        t = string.Template(SEARCH_URL)
        self.search_url = t.substitute({"area_type": area_type})
        t = string.Template(FEATURE_INFO_URL)
        self.feature_info_url = t.substitute({"area_type": area_type})
        
        if not self.media_path:
            # self.media_path = os.path.dirname(os.path.realpath(__file__))
            self.media_path = os.getcwd()
        if not os.path.isdir(self.media_path):
            os.makedirs(self.media_path)
        if catalog:
            self.catalog = Catalog(catalog)
            restore = self.catalog.find(self.code)
            if restore:
                self.restore(restore)
                self.log("%s - restored from %s" % (self.code, catalog))
                return
        if not code:
            return

        feature_info = self.download_feature_info()
        if feature_info:
            geometry = self.get_geometry()
            if catalog and geometry:
                self.catalog.update(self)
                self.catalog.close()
        else:
            self.log("Nothing found")


    def restore(self, restore):
        for a in self.save_attrs:
            setattr(self, a, restore[a])
        if self.coord_out:
            setattr(self, "coord_out", self.coord_out)
        setattr(self, "code_id", self.code)
        self.get_geometry()
        self.file_name = self.code.replace(":", "_")

    def get_coord(self):
        if self.xy:
            return self.xy
        center = self.get_center_xy()
        if center:
            return center
        return []

    def get_attrs(self):
        return self.attrs

    def _get_attrs_to_geojson(self):
        if self.attrs:
            for a in self.attrs:
                attr = self.attrs[a]
                if isinstance(attr, basestring):
                    try:
                        attr = attr.encode('utf-8').strip()
                        self.attrs[a] = attr
                    except:
                        pass
        return self.attrs

    def to_geojson_poly(self, with_attrs=False, dumps=True):
        return self.to_geojson("polygon", with_attrs, dumps)

    def to_geojson_center(self, with_attrs=False, dumps=True):
        current_center_status = self.center_only
        self.center_only = True
        to_return = self.to_geojson("point", with_attrs, dumps)
        self.center_only = current_center_status
        return to_return

    def to_geojson(self, geom_type="point", with_attrs=False, dumps=True):
        attrs = False
        if with_attrs:
            attrs = self._get_attrs_to_geojson()
        xy = []
        if self.center_only:
            xy = self.get_center_xy()
            geom_type = "point"
        else: 
            xy = self.xy
        if xy and len(xy):
            feature_collection = coords2geojson(xy, geom_type, self.coord_out, attrs=attrs)
            if feature_collection:
                if dumps:
                    return json.dumps(feature_collection)
                return feature_collection
        return False

    
    def get_center_xy(self):
        center = self.attrs.get("center")
        if center:
            xy = [[[[center["x"], center["y"]]]]]
            return xy
        return False

    def make_request(self, url):
        response = make_request(url, self.with_proxy)
        return response


    def download_feature_info(self):
        try:
            search_url = self.feature_info_url + self.clear_code(self.code)
            self.log("Start downloading area info: %s" % search_url)
            response = self.make_request(search_url)
            resp = response
            data = json.loads(resp)
            if data:
                feature = data.get("feature")
                if feature:
                    attrs = feature.get("attrs")
                    if attrs:
                        self.attrs = attrs
                        self.code_id = attrs["id"]
                    if feature.get("extent"):
                        self.extent = feature["extent"]
                    if feature.get("center"):
                        x = feature["center"]["x"]
                        y = feature["center"]["y"]
                        if self.coord_out == "EPSG:4326":
                            (x, y) = xy2lonlat(x, y)
                        self.center = {"x": x, "y": y}  
                        self.attrs["center"] = self.center
                        self.log("Area info downloaded.")
                return feature
        except TimeoutException:
            raise TimeoutException()
        except Exception as error:
            self.error(error)
        return False

    @staticmethod
    def clear_code(code):
        """remove first nulls from code  xxxx:00xx >> xxxx:xx"""
        return ":".join(map(lambda x: str(int(x)), code.split(":")))

    @staticmethod
    def get_extent_list(extent):
        """convert extent dick to ordered array"""
        return [extent["xmin"], extent["ymin"], extent["xmax"], extent["ymax"]]

    def get_buffer_extent_list(self):
        """add some buffer to ordered extent array"""
        ex = self.extent
        buf = self.buffer
        if ex and ex["xmin"]:
            ex = [ex["xmin"] - buf, ex["ymin"] - buf, ex["xmax"] + buf, ex["ymax"] + buf]
        else:
            self.log("Area has no coordinates")
            # raise NoCoordinatesException()
        return ex


    def get_geometry(self):
        if self.center_only:
            return self.get_center_xy()
        else:
            return self.parse_geometry_from_image()


    def parse_geometry_from_image(self):
        formats = ["png"]
        tmp_dir = os.path.join(self.media_path, "tmp")
        if not os.path.isdir(tmp_dir):
            os.makedirs(tmp_dir)
        for f in formats:
            bbox = self.get_buffer_extent_list()    
            if bbox:
                image = PkkAreaMerger(bbox=self.get_buffer_extent_list(), output_format=f, with_log=self.with_log,
                                        clear_code=self.clear_code(self.code_id), output_dir=tmp_dir, make_request=self.make_request)
                image.download()
                self.image_path = image.merge_tiles()
                self.width = image.real_width
                self.height = image.real_height
                self.image_extent = image.image_extent

                if image:
                    return self.get_image_geometry()


    def get_image_geometry(self):
        """
        get corner geometry array from downloaded image
        [area1],[area2] - may be multipolygon geometry
           |
        [self],[hole_1],[hole_N]     - holes is optional
           |
        [coord1],[coord2],[coord3]   - min 3 coord for polygon
           |
         [x,y]                       - coordinate pair

         Example:
             [[ [ [x,y],[x,y],[x,y] ], [ [x,y],[x,y],[x,y] ], ], [ [x,y],[x,y],[x,y] ], [ [x,y],[x,y],[x,y] ] ]
                -----------------first polygon-----------------  ----------------second polygon--------------
                ----outer contour---   --first hole contour-
        """
        image_xy_corner = self.image_xy_corner = self.get_image_xy_corner()
        if image_xy_corner:
            self.xy = copy.deepcopy(image_xy_corner)
            for geom in self.xy:
                for p in range(len(geom)):
                    geom[p] = self.image_corners_to_coord(geom[p])
            return self.xy
        return []


    def get_image_xy_corner(self):
        """get сartesian coordinates from raster"""
        import cv2

        if not self.image_path:
            return False
        image_xy_corners = []

        try:
            img = cv2.imread(self.image_path, cv2.IMREAD_GRAYSCALE)
            imagem = (255 - img)

            ret, thresh = cv2.threshold(imagem, 10, 128, cv2.THRESH_BINARY)
            try:
                contours, hierarchy = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            except Exception:
                im2, contours, hierarchy = cv2.findContours(thresh, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)

            hierarchy = hierarchy[0]
            hierarhy_contours = [[] for _ in range(len(hierarchy))]
            for fry in range(len(contours)):
                currentContour = contours[fry]
                currentHierarchy = hierarchy[fry]
                cc = []
                # epsilon = 0.0005 * cv2.arcLength(contours[len(contours) - 1], True)
                approx = cv2.approxPolyDP(currentContour, self.epsilon, True)
                if len(approx) > 2:
                    for c in approx:
                        cc.append([c[0][0], c[0][1]])
                    parent_index = currentHierarchy[3]
                    index = fry if parent_index < 0 else parent_index
                    hierarhy_contours[index].append(cc)

            image_xy_corners = [c for c in hierarhy_contours if len(c) > 0]
            return image_xy_corners
        except Exception as ex:
            self.error(ex)
        return image_xy_corners

    def image_corners_to_coord(self, image_xy_corners):
        """calculate spatial coordinates from cartesian"""
        ex = self.get_extent_list(self.image_extent)
        dx = ((ex[2] - ex[0]) / self.width)
        dy = ((ex[3] - ex[1]) / self.height)
        xy_corners = []
        for im_x, im_y in image_xy_corners:
            x = ex[0] + (im_x * dx)
            y = ex[3] - (im_y * dy)
            if self.coord_out == "EPSG:4326":
                (x, y) = xy2lonlat(x, y)
            xy_corners.append([x, y])
        return xy_corners

    def show_plot(self):
        """Development tool"""
        import cv2
        try:
            from matplotlib import pyplot as plt
        except ImportError:
            self.error('Matplotlib is not installed.')
            raise ImportError('Matplotlib is not installed.')

        img = cv2.imread(self.image_path)
        for corners in self.image_xy_corner:
            for x, y in corners:
                cv2.circle(img, (x, y), 3, 255, -1)
        plt.imshow(img), plt.show()

    def log(self, msg):
        if self.with_log:
            print(msg)

    def error(self, msg):
        if self.with_log:
            logger.warning(msg)
