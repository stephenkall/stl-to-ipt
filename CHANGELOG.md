# Changelog

## [0.7.1](https://github.com/stephenkall/stl-to-ipt/compare/v0.7.0...v0.7.1) (2026-11-12)


### Bug Fixes

* model knurled cylinders as smooth circles instead of one rippled profile per section ([#24](https://github.com/stephenkall/stl-to-ipt/issues/24)) ([3aaa410](https://github.com/stephenkall/stl-to-ipt/commit/3aaa4105011b4812801aa662542831746ea574d3))

## [0.7.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.6.0...v0.7.0) (2026-11-12)


### Features

* align tilted parts, drop micro chamfers, repair invalid sections and report conversion progress ([#22](https://github.com/stephenkall/stl-to-ipt/issues/22)) ([30261f2](https://github.com/stephenkall/stl-to-ipt/commit/30261f2b7718bcb830a33233e62563ceee38e88e))

## [0.6.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.5.2...v0.6.0) (2026-11-05)


### Features

* detect and build holes drilled at an angle about the extrusion axis ([0c4ef5c](https://github.com/stephenkall/stl-to-ipt/commit/0c4ef5cc49413da1148d708af5a6cc407dc6f146))
* detect and build holes drilled at an angle about the extrusion axis ([8e2e98c](https://github.com/stephenkall/stl-to-ipt/commit/8e2e98cf031c48b02c4d66afce49c816d25035f0))

## [0.5.2](https://github.com/stephenkall/stl-to-ipt/compare/v0.5.1...v0.5.2) (2026-11-05)


### Bug Fixes

* drill countersunk holes on into the cavity they open into ([39d1599](https://github.com/stephenkall/stl-to-ipt/commit/39d159994d97ba3b8998afc9b8d24688bed7adb2))
* drill countersunk holes on into the cavity they open into ([cfd0ca5](https://github.com/stephenkall/stl-to-ipt/commit/cfd0ca5d978f80cb425bcff24fbf4d3980565fd2))

## [0.5.1](https://github.com/stephenkall/stl-to-ipt/compare/v0.5.0...v0.5.1) (2026-11-05)


### Bug Fixes

* give back the wall of a tube behind a wide countersink ([5ac63d6](https://github.com/stephenkall/stl-to-ipt/commit/5ac63d68d32ef9f9b3f2b89a0cb8067568627ae2))
* give back the wall of a tube behind a wide countersink ([cd7708b](https://github.com/stephenkall/stl-to-ipt/commit/cd7708b157a71df808631fce1df53092f8fdf73f))

## [0.5.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.4.1...v0.5.0) (2026-11-04)


### Features

* detect countersunk holes and circular recesses on every axis ([ea1d879](https://github.com/stephenkall/stl-to-ipt/commit/ea1d879cc82bf176a2d9f9b8579666a1131e9442))
* detect countersunk holes and circular recesses on every axis ([7e33404](https://github.com/stephenkall/stl-to-ipt/commit/7e334046b5931ce958020d9d4de1e388df021f26))

## [0.4.1](https://github.com/stephenkall/stl-to-ipt/compare/v0.4.0...v0.4.1) (2026-11-04)


### Bug Fixes

* make Ctrl+C responsive and stop the queue on the first press ([a265fbc](https://github.com/stephenkall/stl-to-ipt/commit/a265fbc66f819f9c62b7087963b622e1e74f7975))
* make Ctrl+C responsive and stop the queue on the first press ([d2aca2c](https://github.com/stephenkall/stl-to-ipt/commit/d2aca2c13cbcdc31aca18b3547566e5dbb2336ce))

## [0.4.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.3.0...v0.4.0) (2026-11-04)


### Features

* build gear sectors as a full gear that is trimmed to the part ([9f6e507](https://github.com/stephenkall/stl-to-ipt/commit/9f6e507d3da7c9629840bbe56100611a186b3f91))
* build gear sectors as a full gear that is trimmed to the part ([61debf9](https://github.com/stephenkall/stl-to-ipt/commit/61debf9e433b8621c3a041035868ba75211aadd1))
* detect rounded base edges and build them as a fillet feature ([7e082a6](https://github.com/stephenkall/stl-to-ipt/commit/7e082a62cce96332af55d401a72cfffb4f83bc7b))
* detect rounded base edges and build them as a fillet feature ([710106f](https://github.com/stephenkall/stl-to-ipt/commit/710106fee998c8bb62fa993d5d90b482ea636c00))

## [0.3.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.2.0...v0.3.0) (2026-11-04)


### Features

* build patterns and spur gears from their design parameters ([768fb71](https://github.com/stephenkall/stl-to-ipt/commit/768fb71fdc833cedd4d8b4ecbc290088f27fe6a8))
* build patterns and spur gears from their design parameters ([37c213b](https://github.com/stephenkall/stl-to-ipt/commit/37c213bfa7541bbf12f1f4c81989a34fc4bdaf0d))

## [0.2.0](https://github.com/stephenkall/stl-to-ipt/compare/v0.1.0...v0.2.0) (2026-11-04)


### Features

* add design-intent modeling: blind holes, tangent sketches, clean dimensions, fits and mirroring ([ee4644f](https://github.com/stephenkall/stl-to-ipt/commit/ee4644f68c37ce7f59a9d7ea027a2ceee5bcbf12))
* design-intent modeling (blind holes, tangents, clean dimensions, fits, mirroring) ([3b80651](https://github.com/stephenkall/stl-to-ipt/commit/3b8065120ef58e7f3bace3ece93a863d0c1a19c1))

## 0.1.0 (2026-11-04)


### Features

* add STL to Inventor IPT converter ([c75e7ba](https://github.com/stephenkall/stl-to-ipt/commit/c75e7ba30d586054a877c7ea3d70bd135b601552))
* add STL to Inventor IPT converter ([c4ccf34](https://github.com/stephenkall/stl-to-ipt/commit/c4ccf34222db88ef705bd020ae9c535b7fe50f02))
